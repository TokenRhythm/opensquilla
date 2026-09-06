from __future__ import annotations

from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.pending_input_queue import PendingInputRevision, StoredPendingInput
from opensquilla.gateway.adapters.pending_input_queue import GatewayPendingInputQueueAdapter


class _RecordingPort:
    def __init__(self) -> None:
        self.calls = {
            name: AsyncMock(return_value={"status": name})
            for name in ("enqueue", "list", "update", "reorder", "cancel", "dispatch", "steer")
        }

    def owner_lock(self, pending_id):
        return nullcontext()

    def session_lock(self, key):
        return nullcontext()

    def control_commands(self):
        return frozenset({"/plan"})

    async def current_session_id(self, key):
        return "session-one"

    def fingerprint(self, turn, confirmed):
        return "fingerprint"

    async def insert_pending(self, command, fingerprint):
        await self.calls["enqueue"](command)
        turn = command.turn
        self.row = StoredPendingInput(
            command.pending_input_id,
            turn.session_key,
            turn.source_scope,
            turn.client_request_id,
            turn.client_message_id,
            fingerprint,
            1,
            turn,
            {"pendingInputId": command.pending_input_id},
        )
        return self.row, False

    async def load_pending(self, pending_id):
        return self.row

    async def list_items(self, key: str):
        await self.calls["list"](key)
        return []

    async def reposition(self, key: str, pending_id: str, revision: int, position: int):
        await self.calls["update"](key, pending_id, revision, position)
        return {"pendingInputId": pending_id, "revision": revision + 1, "position": position}

    async def reorder_durable(self, key: str, revisions: tuple[PendingInputRevision, ...]):
        await self.calls["reorder"](key, revisions)
        return []

    def cancellation_lock(self, pending_input_id: str):
        return nullcontext()

    async def cancellation_material_scopes(self, key: str, pending_input_id: str) -> set[str]:
        return set()

    async def cancel_durable(self, key: str, pending_input_id: str, revision: int | None) -> bool:
        await self.calls["cancel"](key, pending_input_id, revision)
        return True

    async def cleanup_promotions(self, key: str, pending_input_id: str, scopes: set[str]) -> None:
        pass

    def cleanup_material(self, pending_input_id: str, scopes: set[str]) -> None:
        pass


class _TurnApplication:
    def __init__(self, port):
        self.port = port

    async def admit(self, command):
        return await self.port.calls["dispatch"](command)

    async def steer(self, command):
        return await self.port.calls["steer"](command)


def _adapter() -> tuple[GatewayPendingInputQueueAdapter, _RecordingPort]:
    port = _RecordingPort()
    return GatewayPendingInputQueueAdapter(port, turns=_TurnApplication(port)), port


async def test_adapter_preserves_queue_identity_and_revision_aliases() -> None:
    adapter, port = _adapter()

    result = await adapter.update(
        {
            "key": "agent:main:webchat:one",
            "pending_input_id": "pending-1",
            "expected_revision": 2,
            "position": 1,
        }
    )

    assert result == {
        "status": "updated",
        "pendingInputId": "pending-1",
        "revision": 3,
        "position": 1,
    }
    assert port.calls["update"].await_args.args == (
        "agent:main:webchat:one",
        "pending-1",
        2,
        1,
    )


async def test_adapter_exposes_all_seven_queue_use_cases() -> None:
    adapter, port = _adapter()
    key = "agent:main:webchat:one"
    identified = {
        "key": key,
        "pendingInputId": "pending-1",
        "message": "text",
        "clientRequestId": "request-one",
        "clientMessageId": "message-one",
        "requestFingerprint": "fingerprint",
        "expectedTurnId": "turn-one",
    }
    revisioned = {**identified, "expectedRevision": 1}

    await adapter.enqueue(identified)
    await adapter.list({"key": key})
    await adapter.update({**revisioned, "position": 0})
    await adapter.reorder(
        {
            "key": key,
            "items": [
                {"pendingInputId": "pending-1", "expectedRevision": 1},
                {"pending_input_id": "pending-2", "expected_revision": 2},
            ],
        }
    )
    await adapter.cancel(identified)
    await adapter.dispatch(identified)
    await adapter.steer(revisioned)

    assert all(call.await_count == 1 for call in port.calls.values())


@pytest.mark.parametrize("missing", [False, True])
async def test_update_preserves_missing_and_revision_conflict_wire_errors(missing: bool) -> None:
    from opensquilla.application.pending_input_queue import (
        PendingInputConflictError,
        PendingInputMissingError,
    )
    from opensquilla.gateway.rpc.registry import RpcHandlerError

    class StoragePort(_RecordingPort):
        async def reposition(self, key, pending_id, revision, position):
            assert (key, pending_id, revision, position) == (
                "agent:main:webchat:one",
                "pending-1",
                2,
                1,
            )
            raise PendingInputMissingError if missing else PendingInputConflictError

    adapter = GatewayPendingInputQueueAdapter(StoragePort())
    with pytest.raises(RpcHandlerError) as raised:
        await adapter.update(
            {
                "key": "agent:main:webchat:one",
                "pending_input_id": "pending-1",
                "expected_revision": 2,
                "position": 1,
            }
        )
    assert raised.value.code == ("PENDING_INPUT_NOT_FOUND" if missing else "PENDING_INPUT_CONFLICT")
    assert raised.value.retryable is (not missing)
    assert raised.value.accepted is False


async def test_reorder_rejects_duplicate_normalized_ids_before_atomic_storage() -> None:
    adapter, port = _adapter()
    with pytest.raises(ValueError, match="unique"):
        await adapter.reorder(
            {
                "key": "agent:main:webchat:one",
                "items": [
                    {"pendingInputId": " pending-1 ", "expectedRevision": 1},
                    {"pending_input_id": "pending-1", "expected_revision": 1},
                ],
            }
        )
    port.calls["reorder"].assert_not_awaited()


async def test_queue_codec_preserves_staged_fingerprint_and_source_capture_policy() -> None:
    from opensquilla.gateway.pending_input_primitives import pending_input_payload
    from opensquilla.gateway.turn_ingress import request_fingerprint

    adapter, port = _adapter()
    raw = {
        "key": "agent:main:webchat:one",
        "pendingInputId": "pending-1",
        "message": "/literal text",
        "display_text": "//literal text",
        "client_request_id": " request-one ",
        "client_message_id": " message-one ",
        "attachments": None,
        "prompt_annotation_ids": [" annotation-one "],
        "_source": {
            "caller_kind": "cli",
            "channel_kind": "cli",
            "surface_id": "terminal-one",
            "noMemoryCapture": "yes",
            "inputProvenance": {"kind": "synthetic-import"},
        },
    }
    await adapter.enqueue(raw)
    command = port.calls["enqueue"].await_args.args[0]
    payload = pending_input_payload(command.turn, command.confirmed_plain_text)
    expected = {
        "key": raw["key"],
        "message": raw["message"],
        "attachments": [],
        "queueMode": "followup",
        "clientRequestId": "request-one",
        "clientMessageId": "message-one",
        "displayText": "//literal text",
        "promptAnnotationIds": ["annotation-one"],
    }
    assert {key: value for key, value in payload.items() if key != "_source"} == expected
    assert request_fingerprint(payload) == request_fingerprint(expected)
    assert command.turn.source_scope == "cli:cli:operator"
    assert payload["_source"]["no_memory_capture"] is True
    assert payload["_source"]["input_provenance"] == {"kind": "synthetic-import"}
    assert payload["_source"]["surface_id"] == "terminal-one"


async def test_queue_gateway_maps_steer_commit_conflict_without_safe_fallback() -> None:
    from opensquilla.application.turn_steering import SteeringAcceptanceError
    from opensquilla.gateway.rpc.registry import RpcHandlerError
    from opensquilla.session.storage import PendingChatInputConflictError

    adapter, port = _adapter()
    raw = {
        "key": "agent:main:webchat:one",
        "pendingInputId": "pending-1",
        "message": "text",
        "clientRequestId": "request-one",
        "clientMessageId": "message-one",
        "requestFingerprint": "fingerprint",
        "expectedTurnId": "turn-one",
        "expectedRevision": 1,
    }
    await adapter.enqueue(raw)
    port.calls["steer"].side_effect = SteeringAcceptanceError(PendingChatInputConflictError())
    with pytest.raises(RpcHandlerError) as caught:
        await adapter.steer(raw)
    assert caught.value.code == "PENDING_INPUT_CONFLICT"
    assert caught.value.details["fallback_safe"] is False
    assert caught.value.retryable is True
    assert caught.value.accepted is False


async def test_queue_gateway_maps_admission_rejection_without_consuming_material() -> None:
    from opensquilla.application.admission_errors import AdmissionError
    from opensquilla.gateway.rpc.registry import RpcHandlerError

    adapter, port = _adapter()
    raw = {
        "key": "agent:main:webchat:one",
        "pendingInputId": "pending-1",
        "message": "text",
        "clientRequestId": "request-one",
        "clientMessageId": "message-one",
        "requestFingerprint": "fingerprint",
    }
    await adapter.enqueue(raw)
    port.calls["dispatch"].side_effect = AdmissionError(
        "STORAGE_BUSY",
        "Session storage is temporarily busy. Retry this send.",
        details={"operation": "accept_turn"},
        retryable=True,
        retry_after_ms=75,
        accepted=False,
    )
    port.cleanup_promotions = AsyncMock()
    with pytest.raises(RpcHandlerError) as caught:
        await adapter.dispatch(raw)
    assert caught.value.code == "STORAGE_BUSY"
    assert caught.value.details == {"operation": "accept_turn"}
    assert caught.value.retryable is True
    assert caught.value.retry_after_ms == 75
    assert caught.value.accepted is False
    port.cleanup_promotions.assert_not_awaited()
