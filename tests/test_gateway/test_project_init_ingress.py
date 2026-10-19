"""Project init keeps explicit intent through normal admission and durable queueing."""

from __future__ import annotations

import pytest

from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.pending_input_primitives import (
    GatewayPendingInputPrimitives,
    pending_input_payload,
    stored_pending_input,
)
from opensquilla.gateway.rpc import get_dispatcher
from opensquilla.gateway.turn_ingress import request_fingerprint
from opensquilla.session.storage import SessionStorage
from tests.test_gateway.test_turn_ingress_rpc import SESSION_KEY, _open_real_stack

COMMAND = {"name": "project.init", "args": "Keep the existing deployment notes."}


@pytest.mark.parametrize("message", ["/init", "//init", "Quote: /init", "`/init`"])
def test_text_and_public_provenance_do_not_infer_init(message):
    turn = decode_admit_turn({
        "key": SESSION_KEY, "message": message,
        "inputProvenance": {"kind": "web_message", "turn_command": COMMAND},
    })
    assert turn.message == message
    assert "turn_command" not in turn.capture.input_provenance


@pytest.mark.parametrize("raw", [
    {}, [], "project.init", {"name": "other", "args": ""},
    {"name": "project.init"}, {"name": "project.init", "args": 1},
    {**COMMAND, "workspace": "/another/project"},
])
def test_invalid_command_cannot_enter_admission(raw):
    with pytest.raises(ValueError, match="turnCommand"):
        decode_admit_turn({"key": SESSION_KEY, "message": "/init", "turnCommand": raw})


def test_web_projection_preserves_command_identity_without_expanding_user_text():
    params = {"message": "/init Keep notes", "turnCommand": COMMAND}
    turn = GatewayTurnAdmissionAdapter.decode_webchat_command(params, SESSION_KEY)
    assert turn.message == params["message"]
    assert turn.capture.input_provenance["turn_command"] == COMMAND
    assert turn.queue_mode == "followup"
    assert turn.request_fingerprint == request_fingerprint(params)
    assert request_fingerprint(params) != request_fingerprint({"message": params["message"]})
    assert request_fingerprint(params) != request_fingerprint({
        **params, "turnCommand": {**COMMAND, "args": "Change the notes"},
    })


async def test_pending_command_survives_database_reopen_and_cannot_be_steered_as_text(tmp_path):
    path = str(tmp_path / "queue.db")
    store = await SessionStorage.open(path)
    turn = decode_admit_turn({
        "key": SESSION_KEY, "message": "/init", "turnCommand": COMMAND,
        "clientRequestId": "init-request", "clientMessageId": "init-message",
    })
    payload = pending_input_payload(turn, False)
    try:
        await store.enqueue_pending_chat_input(
            pending_input_id="init-pending", session_key=SESSION_KEY,
            source_scope=turn.source_scope, client_request_id=turn.client_request_id,
            client_message_id="init-message", request_fingerprint=request_fingerprint(payload),
            payload=payload,
        )
    finally:
        await store.close()
    reopened = await SessionStorage.open(path)
    try:
        row = await reopened.get_pending_chat_input("init-pending")
        assert row is not None
        stored = stored_pending_input(row)
        assert stored.turn.capture.input_provenance["turn_command"] == COMMAND
        assert stored.projection["turnCommand"] == COMMAND
        assert stored.has_non_text_semantics
        assert "/init" not in GatewayPendingInputPrimitives.control_commands()
        assert "/plan" in GatewayPendingInputPrimitives.control_commands()
    finally:
        await reopened.close()


@pytest.mark.parametrize("queued", [False, True])
async def test_init_rpc_replay_preserves_one_user_turn_and_explicit_intent(tmp_path, queued):
    async with _open_real_stack(tmp_path / "init.db") as stack:
        dispatcher = get_dispatcher()
        params = {
            "key": SESSION_KEY, "sessionKey": SESSION_KEY,
            "clientRequestId": "init-request", "clientMessageId": "init-message",
            "message": "/init Keep notes", "turnCommand": COMMAND,
        }
        method = "chat.send"
        if queued:
            params["pendingInputId"] = "init-pending"
            staged = await dispatcher.dispatch(
                "stage", "sessions.pending_inputs.enqueue", params, stack.context,
            )
            assert staged.ok, staged.error
            assert staged.payload["turnCommand"] == COMMAND
            listed = await dispatcher.dispatch(
                "list", "sessions.pending_inputs.list", {"key": SESSION_KEY}, stack.context,
            )
            assert listed.ok, listed.error
            assert listed.payload["items"][0]["turnCommand"] == COMMAND
            params = {
                "key": SESSION_KEY, "pendingInputId": "init-pending",
                "clientRequestId": "init-request",
                "requestFingerprint": staged.payload["requestFingerprint"],
            }
            method = "sessions.pending_inputs.dispatch"
        accepted = await dispatcher.dispatch("accept", method, params, stack.context)
        assert accepted.ok, accepted.error
        await stack.wait_until_running()
        replay = await dispatcher.dispatch("replay", method, params, stack.context)
        assert replay.ok, replay.error
        assert replay.payload["replayed"] is True
        assert stack.received_runs[0].input_provenance["turn_command"] == COMMAND
        assert stack.received_runs[0].message == "/init Keep notes"
        users = [entry for entry in await stack.storage.get_canonical_transcript(stack.session_id)
                 if entry.role == "user"]
        assert len(users) == 1
        assert users[0].content == "/init Keep notes"
