"""Request identity describes original input, never a guarded display projection."""

from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.admission_errors import AdmissionError
from opensquilla.application.admission_views import AdmissionStorageCapabilities
from opensquilla.application.turn_acceptance import DurableTurnAdmission
from opensquilla.application.turn_acceptance_ports import AdmissionPolicy
from opensquilla.gateway.adapters.turn_admission import GatewayTurnAdmissionAdapter
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.input_normalization import LARGE_PASTE_CHARS, normalize_incoming_text
from opensquilla.gateway.turn_ingress import request_fingerprint
from opensquilla.run_mode import RunMode
from opensquilla.session.models import TurnIngressReceipt
from opensquilla.session.storage import TurnAcceptanceResult


@pytest.mark.parametrize("alias", ["initialRoutingMode", "initial_routing_mode"])
@pytest.mark.parametrize("mode", [None, "direct", "router", "ensemble"])
def test_initial_routing_modes_are_decoded_before_admission(alias, mode):
    command = decode_admit_turn({"key": "agent:main:synthetic", "message": "hello", alias: mode})
    assert command.initial_routing_mode == mode


@pytest.mark.parametrize("alias", ["initialRoutingMode", "initial_routing_mode"])
async def test_unknown_initial_routing_mode_never_reaches_admission(alias):
    application = SimpleNamespace(admit=AsyncMock())
    with pytest.raises(ValueError, match="initialRoutingMode must be direct, router, or ensemble"):
        await GatewayTurnAdmissionAdapter(application).admit(
            {"key": "agent:main:synthetic", "message": "hello", alias: "unknown"},
            surface="session",
        )
    application.admit.assert_not_awaited()


@pytest.mark.parametrize("surface", ["webchat", "session"])
async def test_large_paste_identity_matches_original_request_on_each_surface(surface):
    application = SimpleNamespace(admit=AsyncMock(return_value={"status": "accepted"}))
    adapter = GatewayTurnAdmissionAdapter(application)
    key_field = "sessionKey" if surface == "webchat" else "key"
    params = {
        key_field: "agent:main:synthetic",
        "message": "A" * LARGE_PASTE_CHARS,
        "clientRequestId": "request-synthetic",
    }
    await adapter.admit(params, surface=surface)
    first = application.admit.await_args.args[0]
    await adapter.admit(dict(params), surface=surface)
    replay = application.admit.await_args.args[0]
    await adapter.admit({**params, "message": "B" * LARGE_PASTE_CHARS}, surface=surface)
    changed = application.admit.await_args.args[0]

    assert first.request_fingerprint == request_fingerprint(params)
    assert replay.request_fingerprint == first.request_fingerprint
    assert changed.request_fingerprint != first.request_fingerprint
    assert first.message == params["message"]


def test_explicit_fingerprint_payload_keeps_original_shape():
    payload = {
        "key": "agent:main:synthetic",
        "message": "synthetic provider projection",
        "clientRequestId": "request-synthetic",
        "intent": "new_chat",
    }
    original = {"message": "A" * LARGE_PASTE_CHARS, "attachments": []}
    command = decode_admit_turn(payload, fingerprint_params=original)
    assert command.request_fingerprint == request_fingerprint(original)
    assert command.message == payload["message"]
    assert original == {"message": "A" * LARGE_PASTE_CHARS, "attachments": []}


@pytest.mark.parametrize("document", [False, True])
def test_identity_retains_annotation_and_document_alias_normalization(document):
    params = {"key": "agent:main:synthetic", "message": "edit"}
    if document:
        params["document_context"] = {"document_id": " doc ", "head_revision_id": " rev "}
        expected = {
            "message": "edit",
            "documentContext": {
                "documentId": "doc",
                "headRevisionId": "rev",
            },
        }
    else:
        params["prompt_annotation_ids"] = [" annotation "]
        expected = {"message": "edit", "promptAnnotationIds": ["annotation"]}
    assert decode_admit_turn(params).request_fingerprint == request_fingerprint(expected)


async def test_changed_large_paste_conflicts_with_existing_receipt_before_projection(tmp_path):
    params = {
        "key": "agent:main:synthetic",
        "message": "A" * LARGE_PASTE_CHARS,
        "clientRequestId": "request-synthetic",
    }
    original = decode_admit_turn(params)
    changed = decode_admit_turn({**params, "message": "B" * LARGE_PASTE_CHARS})
    acceptance = TurnAcceptanceResult(
        TurnIngressReceipt(
            source_scope=original.source_scope,
            request_session_key=original.session_key,
            client_request_id=original.client_request_id,
            request_fingerprint=original.request_fingerprint,
            accepted_session_key=original.session_key,
            session_id="session-synthetic",
            message_id="message-synthetic",
            task_id="turn-synthetic",
        ),
        replayed=True,
        fresh_user_session=False,
    )
    projection = AsyncMock(return_value={"status": "accepted", "replayed": True})
    ports = SimpleNamespace(
        is_owner=False,
        sessions=object(),
        storage=SimpleNamespace(
            capabilities=AdmissionStorageCapabilities(
                receipts=True, meta_controls=False, atomic_acceptance=False
            ),
            replay_turn_ingress_receipt=AsyncMock(return_value=acceptance),
        ),
        policy=AdmissionPolicy(tmp_path, True, None, None, True, RunMode.SAFE, RunMode.SAFE),
        explicit_ingress_intent=lambda _key: nullcontext(),
        authority_scope=nullcontext,
        normalize_input=lambda command: normalize_incoming_text(
            command.message,
            source_hint={"caller_kind": "web"},
            attachments=[],
        ),
        accepted_response=projection,
    )
    application = DurableTurnAdmission(ports)
    with pytest.raises(AdmissionError) as caught:
        await application.admit(changed)
    assert caught.value.kind == "IDEMPOTENCY_CONFLICT"
    assert caught.value.accepted is False
    projection.assert_not_awaited()
    assert await application.admit(decode_admit_turn(params)) == {
        "status": "accepted",
        "replayed": True,
    }
    projection.assert_awaited_once()
