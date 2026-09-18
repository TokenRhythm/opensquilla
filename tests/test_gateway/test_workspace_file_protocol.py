from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.chat.history import transcript_entries_to_chat_messages
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.pending_input_primitives import pending_input_payload
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.gateway.turn_ingress import request_fingerprint


def _ref():
    return {
        "workspaceId": "project-identity", "relativePath": "docs/note.txt",
        "name": "note.txt", "mime": "text/plain", "size": 42,
    }


@pytest.mark.parametrize("surface", ["webchat", "session"])
async def test_workspace_files_cross_both_admission_aliases(surface):
    application = SimpleNamespace(admit=AsyncMock(return_value={"accepted": True}))
    adapter = GatewayTurnAdmissionAdapter(application, is_owner=True)
    await adapter.admit({
        "key": "agent:main:webchat:synthetic", "message": "Read the selected file",
        "workspaceFiles": [_ref()], "clientRequestId": "synthetic-request",
    }, surface=surface)
    command = application.admit.call_args.args[0]
    assert command.workspace_files == (_ref(),)
    assert not command.attachments


def test_queue_round_trip_and_fingerprint_keep_live_identity():
    params = {
        "key": "agent:main:webchat:synthetic", "message": "Read the selected file",
        "workspaceFiles": [_ref()], "clientRequestId": "synthetic-request",
    }
    original = decode_admit_turn(params)
    queued = pending_input_payload(original, False)
    restored = decode_admit_turn(queued)
    assert restored.workspace_files == original.workspace_files
    assert not restored.attachments
    assert request_fingerprint(params) != request_fingerprint({
        **params, "workspaceFiles": [{**_ref(), "workspaceId": "another-project"}],
    })
    assert request_fingerprint(params) != request_fingerprint({
        **params, "workspaceFiles": [{**_ref(), "relativePath": "another.txt"}],
    })


def test_transcript_has_live_refs_without_snapshot_bytes(tmp_path):
    content, writes = build_transcript_attachment_envelope(
        text="Read the selected file", display_text=None, attachments=[],
        workspace_files=[_ref()], session_id="synthetic-session", media_root=tmp_path,
        persist_enabled=True,
    )
    envelope = json.loads(content)
    assert envelope["workspace_files"] == [_ref()]
    assert envelope["attachments"] == []
    assert writes == []
    assert not list(tmp_path.iterdir())
    messages = transcript_entries_to_chat_messages([
        SimpleNamespace(role="user", content=content, created_at=1, message_id="synthetic-message"),
    ])
    assert messages[0]["workspaceFiles"] == [_ref()]
    projected = messages[0]["attachments"][0]
    assert projected["workspaceFile"] == _ref()
    assert "data" not in projected and "sha256_ref" not in projected
