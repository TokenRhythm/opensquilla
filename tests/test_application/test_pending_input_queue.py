from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, field, replace
from typing import Any

import pytest

from opensquilla.application.pending_input_queue import (
    CancelPendingInput,
    DispatchPendingInput,
    EnqueuePendingInput,
    MovePendingInput,
    PendingDispatchIdentity,
    PendingDispatchReplay,
    PendingEnqueueRejectedError,
    PendingInputQueue,
    PendingInputRevision,
    PendingMaterialRejectedError,
    PendingQueueRejectedError,
    ReorderPendingInputs,
    StagedPendingAttachments,
    SteerPendingInput,
    StoredPendingInput,
)
from opensquilla.application.turn_admission import AdmitTurn


async def test_queue_moves_use_revision_cas_and_leave_stale_order_unchanged() -> None:
    from opensquilla.application.pending_input_queue import (
        MovePendingInput,
        PendingInputConflictError,
    )

    class QueueStorage:
        def __init__(self):
            self.row = {"pendingInputId": "one", "position": 0, "revision": 2}

        async def list_items(self, key):
            assert key == "agent:main:webchat:one"
            return [dict(self.row)]

        async def reposition(self, key, pending_id, revision, position):
            assert key == "agent:main:webchat:one" and pending_id == "one"
            if self.row["revision"] != revision:
                raise PendingInputConflictError
            self.row = {**self.row, "revision": revision + 1, "position": position}
            return dict(self.row)

    queue = PendingInputQueue(QueueStorage())
    moved = await queue.update(MovePendingInput(" agent:main:webchat:one ", " one ", 2, 1))
    assert moved == {"status": "updated", "pendingInputId": "one", "position": 1, "revision": 3}
    with pytest.raises(PendingInputConflictError):
        await queue.update(MovePendingInput("agent:main:webchat:one", "one", 2, 0))
    assert (await queue.list("agent:main:webchat:one"))["items"] == [
        {"pendingInputId": "one", "position": 1, "revision": 3}
    ]


@dataclass
class _QueueCall:
    session_key: str
    pending_input_id: str | None = None
    expected_revision: int | None = None


@dataclass
class _Port:
    calls: list[tuple[str, _QueueCall]] = field(default_factory=list)

    async def _call(self, name: str, request: _QueueCall) -> dict[str, Any]:
        self.calls.append((name, request))
        return {"operation": name, "sessionKey": request.session_key}

    async def list_items(self, key: str):
        await self._call("list", _QueueCall(key))
        return []

    async def reposition(self, key: str, pending_id: str, revision: int, position: int):
        await self._call("update", _QueueCall(key, pending_id, revision))
        return {"pendingInputId": pending_id, "revision": revision + 1, "position": position}

    async def reorder_durable(self, key: str, revisions: tuple[PendingInputRevision, ...]):
        await self._call("reorder", _QueueCall(key))
        return []

    def cancellation_lock(self, pending_input_id: str):
        return nullcontext()

    async def cancellation_material_scopes(self, key: str, pending_input_id: str) -> set[str]:
        return {"session-one"}

    async def cancel_durable(self, key: str, pending_input_id: str, revision: int | None) -> bool:
        await self._call("cancel", _QueueCall(key, pending_input_id, revision))
        return True

    async def cleanup_promotions(self, key: str, pending_input_id: str, scopes: set[str]) -> None:
        pass

    def cleanup_material(self, pending_input_id: str, scopes: set[str]) -> None:
        pass


async def test_queue_canonicalizes_identity_for_all_explicit_use_cases() -> None:
    class FullPort(_PrimitivePort, _Port):
        def __init__(self):
            _PrimitivePort.__init__(self)
            _Port.__init__(self)

    port = FullPort()
    turns = _Turns(port)
    queue = PendingInputQueue(port, turns=turns)
    key = " agent:main:webchat:one "
    await queue.enqueue(EnqueuePendingInput(_turn(session_key=key), " pending-1 "))
    await queue.list(key)
    await queue.update(MovePendingInput(key, "pending-1", 2, 0))
    await queue.reorder(
        ReorderPendingInputs(
            key,
            (
                PendingInputRevision("pending-1", 2),
                PendingInputRevision("pending-2", 1),
            ),
        )
    )
    await queue.cancel(CancelPendingInput(key, "pending-1", 2))
    await queue.dispatch(
        DispatchPendingInput(
            key, "pending-1", "request-one", "text-fingerprint", "web:web:operator"
        )
    )
    await queue.steer(
        SteerPendingInput(
            key,
            "pending-1",
            "request-one",
            "message-one",
            "text-fingerprint",
            2,
            "web:web:operator",
            "turn-one",
        )
    )

    assert [name for name, _request in port.calls] == ["list", "update", "reorder", "cancel"]
    assert all(request.session_key == "agent:main:webchat:one" for _name, request in port.calls)
    assert port.row.pending_input_id == "pending-1"
    assert len(turns.admissions) == len(turns.steers) == 1
    assert (
        turns.admissions[0].session_key == turns.steers[0].session_key == "agent:main:webchat:one"
    )


async def test_revision_guard_rejects_update_before_storage() -> None:
    port = _Port()
    queue = PendingInputQueue(port)

    with pytest.raises(ValueError, match="expected_revision"):
        await queue.update(MovePendingInput("agent:main:webchat:one", "pending-1", 0, 1))

    assert port.calls == []


@pytest.mark.parametrize("conflict", [False, True])
async def test_pending_cancel_cleans_material_only_after_durable_cas(conflict: bool) -> None:
    operations: list[str] = []

    class MaterialPort(_Port):
        async def cancel_durable(
            self, key: str, pending_input_id: str, revision: int | None
        ) -> bool:
            assert revision == 2
            operations.append("commit")
            if conflict:
                raise ValueError("changed revision")
            return True

        async def cleanup_promotions(
            self, key: str, pending_input_id: str, scopes: set[str]
        ) -> None:
            assert scopes == {"session-one"}
            operations.append("promotions")

        def cleanup_material(self, pending_input_id: str, scopes: set[str]) -> None:
            operations.append("material")

    queue = PendingInputQueue(MaterialPort())
    request = CancelPendingInput("agent:main:webchat:one", "pending-one", 2)
    if conflict:
        with pytest.raises(ValueError, match="changed revision"):
            await queue.cancel(request)
        assert operations == ["commit"]
    else:
        result = await queue.cancel(request)
        assert result["cancelled"] is True
        assert operations == ["commit", "promotions", "material"]


class _PrimitivePort:
    def __init__(self):
        self.operations = []
        self.row = None
        self.tombstone = None
        self.replay = None
        self.manifest = False
        self.stage_error = None
        self.insert_error = None
        self.stage_entered = None
        self.stage_release = None

    @asynccontextmanager
    async def owner_lock(self, pending_id):
        self.operations.append(("owner", pending_id))
        yield

    @asynccontextmanager
    async def session_lock(self, key):
        self.operations.append(("session", key))
        yield

    async def current_session_id(self, key):
        self.operations.append(("scope", key))
        return "current-session"

    def control_commands(self):
        return frozenset({"/plan", "/reset"})

    def has_recovery_manifest(self, scope, pending_id):
        self.operations.append(("manifest", scope))
        return self.manifest

    def fingerprint(self, turn, confirmed):
        return "staged-fingerprint" if turn.attachments else "text-fingerprint"

    async def stage_attachments(self, scope, pending_id, attachments, fingerprint):
        self.operations.append(("stage", fingerprint))
        if self.stage_entered is not None:
            self.stage_entered.set()
            await self.stage_release.wait()
        if self.stage_error:
            raise self.stage_error
        return StagedPendingAttachments(attachments, ("upload-one",))

    async def insert_pending(self, command, fingerprint):
        self.operations.append(("insert", fingerprint))
        if self.insert_error:
            raise self.insert_error
        turn = command.turn
        self.row = StoredPendingInput(
            command.pending_input_id,
            turn.session_key,
            turn.source_scope,
            turn.client_request_id,
            turn.client_message_id,
            fingerprint,
            2,
            turn,
            {"pendingInputId": command.pending_input_id, "revision": 2},
            frozenset({"material-session"}),
        )
        return self.row, False

    async def pending_exists(self, pending_id):
        return self.row is not None

    async def load_pending(self, pending_id):
        self.operations.append(("load", pending_id))
        return self.row

    async def dispatch_identity(self, pending_id):
        self.operations.append(("tombstone", pending_id))
        return self.tombstone

    async def replay_dispatch(self, source, key, request_id):
        self.operations.append(("replay", request_id))
        return self.replay

    def cleanup_material(self, pending_id, scopes):
        self.operations.append(("cleanup", frozenset(scopes)))

    async def evict_upload(self, upload_id):
        self.operations.append(("evict", upload_id))


class _Turns:
    def __init__(self, port):
        self.port = port
        self.admissions = []
        self.steers = []

    async def admit(self, command):
        self.admissions.append(command)
        self.port.operations.append(("accept", command.pending_input))
        return {"accepted": True, "task_id": "task-one"}

    async def steer(self, command):
        self.steers.append(command)
        self.port.operations.append(("steer", command.pending_input))
        return {"accepted": True, "expected_turn_id": command.expected_turn_id}


def _turn(**changes):
    return replace(
        AdmitTurn(
            session_key="agent:main:webchat:one",
            message="Queued text",
            surface="session",
            client_request_id="request-one",
            client_message_id="message-one",
            source_scope="web:web:operator",
        ),
        **changes,
    )


async def test_enqueue_owns_lock_stage_commit_evict_order_and_shields_cancellation():
    port = _PrimitivePort()
    port.stage_entered, port.stage_release = asyncio.Event(), asyncio.Event()
    queue = PendingInputQueue(port)
    command = EnqueuePendingInput(_turn(attachments=({"file_uuid": "upload-one"},)), "pending-one")
    task = asyncio.create_task(queue.enqueue(command))
    await port.stage_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    port.stage_release.set()
    result = await task
    assert result["status"] == "staged"
    assert [step for step, _ in port.operations] == [
        "owner",
        "session",
        "scope",
        "manifest",
        "stage",
        "insert",
        "evict",
    ]


@pytest.mark.parametrize("manifest", [False, True])
async def test_enqueue_known_failure_never_deletes_a_prior_recovery_owner(manifest):
    port = _PrimitivePort()
    port.manifest = manifest
    port.insert_error = PendingEnqueueRejectedError("conflict")
    queue = PendingInputQueue(port)
    with pytest.raises(PendingEnqueueRejectedError):
        await queue.enqueue(
            EnqueuePendingInput(_turn(attachments=({"file_uuid": "one"},)), "pending-one")
        )
    assert any(name == "cleanup" for name, _ in port.operations) is not manifest
    assert not any(name == "evict" for name, _ in port.operations)


@pytest.mark.parametrize(
    "reason,cleanup", [("expired", True), ("corrupt", False), ("conflict", False)]
)
async def test_enqueue_material_failure_preserves_ambiguous_owners(reason, cleanup):
    port = _PrimitivePort()
    port.stage_error = PendingMaterialRejectedError(reason, "synthetic material failure")
    queue = PendingInputQueue(port)
    with pytest.raises(PendingMaterialRejectedError):
        await queue.enqueue(
            EnqueuePendingInput(_turn(attachments=({"file_uuid": "one"},)), "pending-one")
        )
    assert any(name == "cleanup" for name, _ in port.operations) is cleanup
    assert not any(name == "insert" for name, _ in port.operations)


async def test_dispatch_tombstone_replay_never_readmits_and_cleans_accepted_and_current_scopes():
    port = _PrimitivePort()
    turns = _Turns(port)
    port.tombstone = PendingDispatchIdentity(
        "agent:main:webchat:one",
        "web:web:operator",
        "request-one",
        "message-one",
        "fingerprint",
    )
    port.replay = PendingDispatchReplay(
        "fingerprint", "accepted-session", {"accepted": True, "replayed": True}
    )
    queue = PendingInputQueue(port, turns=turns)
    command = DispatchPendingInput(
        "agent:main:webchat:one", "pending-one", "request-one", "fingerprint", "web:web:operator"
    )
    assert (await queue.dispatch(command))["replayed"] is True
    assert turns.admissions == []
    assert port.operations[-1] == ("cleanup", frozenset({"current-session", "accepted-session"}))
    port.operations.clear()
    with pytest.raises(PendingQueueRejectedError, match="missing"):
        await queue.dispatch(replace(command, source_scope="cli:cli:operator"))
    assert not any(name in {"cleanup", "replay"} for name, _ in port.operations)


async def test_dispatch_uses_staged_command_and_cas_guard_before_cleanup():
    port = _PrimitivePort()
    turns = _Turns(port)
    queue = PendingInputQueue(port, turns=turns)
    await queue.enqueue(EnqueuePendingInput(_turn(), "pending-one"))
    port.operations.clear()
    await queue.dispatch(
        DispatchPendingInput(
            "agent:main:webchat:one",
            "pending-one",
            "request-one",
            "text-fingerprint",
            "web:web:operator",
        )
    )
    command = turns.admissions[0]
    assert command.message == "Queued text"
    assert command.pending_input.pending_input_id == "pending-one"
    assert command.pending_input.expected_revision == 2
    assert [name for name, _ in port.operations] == ["owner", "load", "accept", "cleanup"]


async def test_pending_steer_validates_revision_before_turn_application_and_uses_staged_text():
    port = _PrimitivePort()
    turns = _Turns(port)
    queue = PendingInputQueue(port, turns=turns)
    await queue.enqueue(EnqueuePendingInput(_turn(), "pending-one"))
    command = SteerPendingInput(
        "agent:main:webchat:one",
        "pending-one",
        "request-one",
        "message-one",
        "text-fingerprint",
        1,
        "web:web:operator",
        "turn-one",
        retry_message="Untrusted retry text",
    )
    with pytest.raises(PendingQueueRejectedError, match="steer-identity"):
        await queue.steer(command)
    assert turns.steers == []
    await queue.steer(replace(command, expected_revision=2))
    admitted = turns.steers[0]
    assert admitted.message == "Queued text"
    assert admitted.pending_input.source_scope == "web:web:operator"
    assert admitted.pending_input.expected_revision == 2
