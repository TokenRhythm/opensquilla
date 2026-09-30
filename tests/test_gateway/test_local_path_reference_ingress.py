"""Explicit local path display metadata preserves the original user input."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.contracts.local_path_references import normalize_local_path_references
from opensquilla.gateway.adapters.pending_input_queue import GatewayPendingInputQueueAdapter
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.pending_input_primitives import (
    pending_input_payload,
    pending_input_projection,
    stored_pending_input,
)
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.gateway.turn_ingress import request_fingerprint
from opensquilla.gateway.turn_steering import decode_steering_command

PATHS = [r"C:\Users\test\Downloads\报告.pdf", "/tmp/report.html", r"\\host\share\report.txt"]
MESSAGE = "Compare these files\n" + "\n".join(PATHS)
PARAMS = {
    "key": "agent:main:local-paths", "message": MESSAGE,
    "clientRequestId": "request-one", "clientMessageId": "message-one",
}


@pytest.mark.parametrize("surface", ["webchat", "session"])
async def test_adapters_preserve_explicit_references_and_fingerprint(surface):
    application = SimpleNamespace(admit=AsyncMock(return_value={"status": "accepted"}))
    await GatewayTurnAdmissionAdapter(application).admit(
        {**PARAMS, "localPathReferences": PATHS}, surface=surface,
    )
    command = application.admit.await_args.args[0]
    assert command.local_path_references == tuple(PATHS)
    assert command.message == MESSAGE
    assert command.request_fingerprint != request_fingerprint(PARAMS)
    assert command.request_fingerprint == request_fingerprint({
        **PARAMS, "localPathReferences": PATHS,
    })


@pytest.mark.parametrize("value", [None, []])
def test_empty_metadata_preserves_legacy_fingerprint(value):
    assert request_fingerprint({
        **PARAMS, "localPathReferences": value,
    }) == request_fingerprint(PARAMS)


@pytest.mark.parametrize("value", [
    "file.txt", ["file.txt"], ["C:relative.txt"], [" /tmp/file.txt"], ["/tmp/file.txt "],
    ["/tmp/a\nb"], ["/tmp/a\x00b"], ["/tmp/a\x7fb"], ["/" + "a" * 32768],
    [123], ["/tmp/not-selected.txt"], list(reversed(PATHS)), [""],
])
def test_rejects_invalid_or_mismatched_metadata(value):
    with pytest.raises(ValueError, match="localPathReferences"):
        decode_admit_turn({**PARAMS, "localPathReferences": value})


def test_normalization_is_bounded_and_does_not_probe_files():
    assert normalize_local_path_references(PATHS, message=MESSAGE) == tuple(PATHS)
    assert normalize_local_path_references(PATHS, message="\n".join(PATHS)) == tuple(PATHS)
    with pytest.raises(ValueError, match="bounded message"):
        normalize_local_path_references(PATHS, message="a" * 100_000 + "\n" + MESSAGE)
    with pytest.raises(ValueError, match="suffix"):
        normalize_local_path_references(["/tmp/a"], message="prefix/tmp/a")


def test_transcript_history_retains_paths_without_material_io(tmp_path):
    content, writes = build_transcript_attachment_envelope(
        text=MESSAGE + "\n\nGenerated browser context", display_text=MESSAGE,
        attachments=[], local_path_references=PATHS, session_id="synthetic",
        media_root=tmp_path, persist_enabled=True,
    )
    assert writes == []
    assert list(tmp_path.iterdir()) == []
    assert json.loads(content)["local_path_references"] == PATHS
    result = transcript_entries_to_chat_messages([
        SimpleNamespace(role="user", content=content, message_id="message-one"),
    ])[0]
    assert result["text"] == MESSAGE
    assert result["localPathReferences"] == PATHS


@pytest.mark.parametrize("value", [None, ["/tmp/not-selected.txt"]])
def test_history_never_guesses_or_hides_unmarked_path_text(value):
    entry = SimpleNamespace(
        role="user", message_id="legacy", content=json.dumps({
            "text": MESSAGE, "local_path_references": value,
        }),
    )
    result = transcript_entries_to_chat_messages([entry])[0]
    assert result["text"] == MESSAGE
    assert "localPathReferences" not in result


def test_pending_roundtrip_keeps_references_as_text_not_attachment_authority():
    adapter = GatewayPendingInputQueueAdapter(SimpleNamespace(), turns=SimpleNamespace())
    command = adapter._enqueue_command({
        **PARAMS, "pendingInputId": "pending-one", "localPathReferences": PATHS,
    })
    payload = pending_input_payload(command.turn, False)
    row = SimpleNamespace(
        pending_input_id="pending-one", session_key=PARAMS["key"],
        client_request_id="request-one", client_message_id="message-one",
        source_scope=command.turn.source_scope, request_fingerprint=request_fingerprint(payload),
        state_revision=1, position=0, created_at=1, updated_at=1, schema_version=1, payload=payload,
    )
    restored = stored_pending_input(row)
    assert restored.turn.local_path_references == tuple(PATHS)
    assert restored.turn.message == MESSAGE
    assert not restored.has_non_text_semantics
    assert not restored.turn.attachments
    assert pending_input_projection(row)["localPathReferences"] == PATHS


def test_steering_keeps_metadata_in_identity_without_disabling_text_steering():
    params = {**PARAMS, "expectedTurnId": "turn-one"}
    plain = decode_steering_command(params, key=PARAMS["key"], principal_role="operator")
    selected = decode_steering_command(
        {**params, "localPathReferences": PATHS}, key=PARAMS["key"], principal_role="operator",
    )
    assert not selected.has_non_text_input
    assert selected.local_path_references == tuple(PATHS)
    assert selected.request_fingerprint != plain.request_fingerprint


async def test_steering_preparation_persists_metadata_but_returns_only_canonical_text(tmp_path):
    from opensquilla.application.turn_steering import SteeringContext
    from opensquilla.gateway.turn_steering import GatewaySteeringPrimitives
    from opensquilla.session.manager import SessionManager
    from opensquilla.session.storage import SessionStorage

    storage = await SessionStorage.open(str(tmp_path / "steer-paths.db"))
    manager = SessionManager(storage, inject_time_prefix=False)
    try:
        session = await manager.create(PARAMS["key"], agent_id="main")
        ports = GatewaySteeringPrimitives(
            session_manager=manager, task_runtime=None,
            emit_steer=AsyncMock(), emit_disposition=AsyncMock(),
        )
        prepared = await ports.prepare(
            PARAMS["key"], MESSAGE,
            SteeringContext("turn-one", "message-one", "web", local_path_references=tuple(PATHS)),
            session,
        )
        assert prepared.message == MESSAGE
        assert json.loads(prepared.entry.content)["local_path_references"] == PATHS
        result = transcript_entries_to_chat_messages([prepared.entry])[0]
        assert result["text"] == MESSAGE
        assert result["localPathReferences"] == PATHS
    finally:
        await storage.close()
