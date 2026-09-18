"""Explicit skills keep their identity across acceptance and durable queues."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.contracts.selected_skills import normalize_selected_skills
from opensquilla.gateway.adapters.pending_input_queue import GatewayPendingInputQueueAdapter
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.pending_input_primitives import (
    pending_input_payload,
    pending_input_projection,
    stored_pending_input,
)
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.task_runtime import _reusable_route_envelope
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.gateway.turn_ingress import request_fingerprint
from opensquilla.gateway.turn_steering import decode_steering_command

REF = {"name": "synthetic-table", "instanceId": "instance-one", "digest": "digest-one"}
PARAMS = {
    "key": "agent:main:synthetic", "message": "Make a table",
    "clientRequestId": "request-one", "clientMessageId": "message-one",
}
WORKSPACE_REF = {
    "workspaceId": "synthetic-project", "relativePath": "notes.txt",
    "name": "notes.txt", "mime": "text/plain",
}


@pytest.mark.parametrize("surface", ["webchat", "session"])
async def test_adapters_keep_bound_skills_and_normalized_fingerprint(surface):
    application = SimpleNamespace(admit=AsyncMock(return_value={"status": "accepted"}))
    adapter = GatewayTurnAdmissionAdapter(application)
    params = {**PARAMS, "selectedSkills": [REF, REF]}
    await adapter.admit(params, surface=surface)
    command = application.admit.await_args.args[0]
    assert command.selected_skills == (REF,)
    assert command.request_fingerprint == request_fingerprint({**params, "selectedSkills": [REF]})
    assert command.request_fingerprint != request_fingerprint({
        **params, "selectedSkills": [{**REF, "digest": "digest-two"}],
    })


@pytest.mark.parametrize("surface", ["webchat", "session"])
async def test_workspace_and_skill_selection_share_admission_identity(surface):
    application = SimpleNamespace(admit=AsyncMock(return_value={"status": "accepted"}))
    adapter = GatewayTurnAdmissionAdapter(application)
    params = {**PARAMS, "selectedSkills": [REF], "workspaceFiles": [WORKSPACE_REF]}
    await adapter.admit(params, surface=surface)
    command = application.admit.await_args.args[0]
    assert command.selected_skills == (REF,)
    assert command.workspace_files == (WORKSPACE_REF,)
    assert command.request_fingerprint == request_fingerprint(params)
    assert command.request_fingerprint != request_fingerprint({
        **params, "workspaceFiles": [{**WORKSPACE_REF, "relativePath": "changed.txt"}],
    })
    assert command.request_fingerprint != request_fingerprint({
        **params, "selectedSkills": [{**REF, "digest": "changed-digest"}],
    })


@pytest.mark.parametrize("value", [[], None])
def test_empty_selection_keeps_old_request_fingerprint(value):
    assert request_fingerprint({**PARAMS, "selectedSkills": value}) == request_fingerprint(PARAMS)


@pytest.mark.parametrize("value", [
    "synthetic-table", [{}], [{**REF, "path": "/unexpected"}], [{**REF, "digest": ""}],
    [REF, {**REF, "instanceId": "other-instance"}], [REF] * 17,
])
def test_malformed_or_conflicting_selection_rejected_before_acceptance(value):
    with pytest.raises(ValueError, match="selectedSkills"):
        decode_admit_turn({**PARAMS, "selectedSkills": value})


def test_normalization_preserves_selection_order_and_does_not_modify_input():
    spaced = {**REF, "name": " synthetic-table "}
    second = {**REF, "name": "synthetic-paper", "instanceId": "instance-two"}
    assert normalize_selected_skills([spaced, second, REF]) == (REF, second)
    assert spaced["name"] == " synthetic-table "


@pytest.mark.parametrize("with_workspace_file", [False, True])
def test_pending_queue_roundtrip_retains_skill_selection_and_blocks_steering(with_workspace_file):
    adapter = GatewayPendingInputQueueAdapter(SimpleNamespace(), turns=SimpleNamespace())
    command = adapter._enqueue_command({
        **PARAMS, "pendingInputId": "pending-one", "selectedSkills": [REF],
        **({"workspaceFiles": [WORKSPACE_REF]} if with_workspace_file else {}),
    })
    assert command.turn.selected_skills == (REF,)
    payload = pending_input_payload(command.turn, False)
    row = SimpleNamespace(
        pending_input_id="pending-one", session_key=PARAMS["key"],
        client_request_id="request-one", client_message_id="message-one",
        source_scope=command.turn.source_scope, request_fingerprint=request_fingerprint(payload),
        state_revision=1, position=0, created_at=1, updated_at=1, schema_version=1, payload=payload,
    )
    restored = stored_pending_input(row)
    assert restored.turn.selected_skills == (REF,)
    assert restored.has_non_text_semantics
    assert pending_input_projection(row)["selectedSkills"] == [REF]
    assert restored.request_fingerprint == row.request_fingerprint
    if with_workspace_file:
        assert restored.turn.workspace_files == (WORKSPACE_REF,)
        assert pending_input_projection(row)["workspaceFiles"] == [WORKSPACE_REF]


def test_skill_bearing_steer_is_non_text_input():
    command = decode_steering_command(
        {**PARAMS, "expectedTurnId": "turn-one", "selectedSkills": [REF]},
        key=PARAMS["key"], principal_role="operator",
    )
    assert command.has_non_text_input


async def test_combined_references_survive_durable_queue_move_reorder_and_dispatch(tmp_path):
    from opensquilla.gateway.rpc import RpcContext
    from opensquilla.gateway.rpc_sessions import _GatewayPendingInputQueuePort
    from opensquilla.session.models import SessionNode
    from opensquilla.session.storage import SessionStorage

    storage = SessionStorage(str(tmp_path / "combined-queue.db"))
    await storage.connect()
    turns = SimpleNamespace(admit=AsyncMock(return_value={"status": "accepted"}))
    context = RpcContext(
        conn_id="synthetic-queue",
        session_manager=SimpleNamespace(storage=storage),
        config=SimpleNamespace(attachments=SimpleNamespace(media_root=str(tmp_path / "media"))),
    )
    adapter = GatewayPendingInputQueueAdapter(_GatewayPendingInputQueuePort(context), turns=turns)
    try:
        await storage.upsert_session(SessionNode(
            session_key=PARAMS["key"], session_id="combined-session", agent_id="main",
            created_at=1, updated_at=1,
        ))
        queued = await adapter.enqueue({
            **PARAMS, "pendingInputId": "combined-pending",
            "workspaceFiles": [WORKSPACE_REF], "selectedSkills": [REF],
        })
        await adapter.enqueue({
            **PARAMS, "pendingInputId": "other-pending",
            "clientRequestId": "other-request", "clientMessageId": "other-message",
        })
        moved = await adapter.update({
            "key": PARAMS["key"], "pendingInputId": "combined-pending",
            "expectedRevision": queued["revision"], "position": 1,
        })
        rows = await storage.list_pending_chat_inputs(PARAMS["key"])
        reordered = await adapter.reorder({
            "key": PARAMS["key"], "items": [
                {"pendingInputId": row.pending_input_id, "expectedRevision": row.state_revision}
                for row in reversed(rows)
            ],
        })
        # Reopen SQLite before dispatch so no in-memory command can supply the references.
        await storage.close()
        await storage.connect()
        restored = await storage.get_pending_chat_input("combined-pending")
        assert restored is not None
        for projection in (queued, moved, *reordered["items"]):
            if projection["pendingInputId"] == "combined-pending":
                assert projection["workspaceFiles"] == [WORKSPACE_REF]
                assert projection["selectedSkills"] == [REF]
                assert projection["requestFingerprint"] == queued["requestFingerprint"]
        assert restored.payload["workspaceFiles"] == [WORKSPACE_REF]
        assert restored.payload["selectedSkills"] == [REF]
        assert restored.request_fingerprint == request_fingerprint(restored.payload)
        await adapter.dispatch({
            "key": PARAMS["key"], "pendingInputId": "combined-pending",
            "clientRequestId": PARAMS["clientRequestId"],
            "requestFingerprint": queued["requestFingerprint"],
        })
        turns.admit.assert_awaited_once()
        admitted = turns.admit.await_args.args[0]
        assert admitted.workspace_files == (WORKSPACE_REF,)
        assert admitted.selected_skills == (REF,)
        assert admitted.request_fingerprint == restored.request_fingerprint
        assert admitted.pending_input.expected_revision == restored.state_revision
    finally:
        await storage.close()


def test_reused_route_does_not_select_skills_for_later_turns():
    envelope = RouteEnvelope(
        SourceKind.WEB, "synthetic", "main", PARAMS["key"],
        metadata={"selected_skills": [REF], "synthetic": "retained"},
    )
    reused = _reusable_route_envelope(envelope)
    assert "selected_skills" not in reused.metadata
    assert reused.metadata["synthetic"] == "retained"
    assert envelope.metadata["selected_skills"] == [REF]
    assert replace(envelope).metadata["selected_skills"] == [REF]


def test_user_transcript_preserves_selected_skills_for_history_and_retry(tmp_path):
    from opensquilla.chat.history import transcript_entries_to_chat_messages

    content, writes = build_transcript_attachment_envelope(
        text="Make a table", attachments=[], selected_skills=[REF], session_id="synthetic",
        media_root=tmp_path, persist_enabled=True,
    )
    assert json.loads(content) == {
        "text": "Make a table", "attachments": [], "selected_skills": [REF],
    }
    assert writes == []
    entry = SimpleNamespace(
        id=1, message_id="message-one", role="user", content=content, created_at="now",
        provenance_kind=None, provenance_source_session_key=None, provenance_source_tool=None,
        turn_usage=None, tool_calls=None,
    )
    messages = transcript_entries_to_chat_messages([entry])
    assert messages[0]["text"] == "Make a table"
    assert messages[0]["selectedSkills"] == [REF]


def test_user_transcript_keeps_upload_workspace_reference_and_selected_skill(tmp_path):
    from opensquilla.chat.history import transcript_entries_to_chat_messages

    content, writes = build_transcript_attachment_envelope(
        text="Compare the synthetic notes", attachments=[{
            "type": "text/plain", "name": "uploaded.txt",
            "data": base64.b64encode(b"Synthetic uploaded content").decode("ascii"),
            "_was_staged": True,
        }],
        workspace_files=[WORKSPACE_REF], selected_skills=[REF], session_id="synthetic",
        media_root=tmp_path, persist_enabled=True,
    )
    payload = json.loads(content)
    assert payload["workspace_files"] == [WORKSPACE_REF]
    assert payload["selected_skills"] == [REF]
    assert len(writes) == 1
    entry = SimpleNamespace(
        id=1, message_id="combined-message", role="user", content=content, created_at="now",
        provenance_kind=None, provenance_source_session_key=None, provenance_source_tool=None,
        turn_usage=None, tool_calls=None,
    )
    projected = transcript_entries_to_chat_messages([entry])[0]
    assert projected["text"] == "Compare the synthetic notes"
    assert projected["workspaceFiles"] == [WORKSPACE_REF]
    assert projected["selectedSkills"] == [REF]
    assert {item["name"] for item in projected["attachments"]} == {"uploaded.txt", "notes.txt"}
