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


@pytest.mark.parametrize(
    "field",
    ["documentContext", "document_context", "promptAnnotationIds", "prompt_annotation_ids"],
)
def test_retired_context_fields_cannot_create_an_executable_turn(field):
    from opensquilla.gateway.rpc import RpcHandlerError

    with pytest.raises(RpcHandlerError) as caught:
        decode_admit_turn({"key": "agent:main:synthetic", "message": "edit", field: {}})
    assert caught.value.code == "DOCUMENT_EDITING_RETIRED"


def test_page_context_identity_uses_normalized_user_content():
    context = {"targetRef": " page-one ", "annotations": [{"text": "make it blue"}]}
    params = {"key": "agent:main:synthetic", "message": "edit", "pageContext": context}
    command = decode_admit_turn(params)
    expected = {"targetRef": "page-one", "annotations": [{"text": "make it blue"}]}
    assert command.page_context == expected
    assert command.request_fingerprint == request_fingerprint({**params, "pageContext": expected})


@pytest.mark.parametrize("change", ["message", "selectedSkills"])
async def test_changed_input_conflicts_with_existing_receipt_before_projection(tmp_path, change):
    params = {
        "key": "agent:main:synthetic",
        "message": "A" * LARGE_PASTE_CHARS,
        "clientRequestId": "request-synthetic",
    }
    if change == "selectedSkills":
        params["selectedSkills"] = [
            {"name": "synthetic-table", "instanceId": "instance-one", "digest": "digest-one"}
        ]
    original = decode_admit_turn(params)
    changed_fields = {"message": "B" * LARGE_PASTE_CHARS} if change == "message" else {
        "selectedSkills": [
            {"name": "synthetic-table", "instanceId": "instance-two", "digest": "digest-two"}
        ]
    }
    changed = decode_admit_turn({**params, **changed_fields})
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


@pytest.mark.parametrize("surface", ["session", "webchat"])
@pytest.mark.parametrize("outcome", ["found", "missing", "changed", "no_request_id", "spaced"])
async def test_retired_requests_only_read_matching_durable_receipts(surface, outcome):
    key = "agent:main:synthetic"
    params = {
        "key" if surface == "session" else "sessionKey": key,
        "message": "Previously accepted input",
        "clientRequestId": "request-synthetic",
        "documentContext": {"documentId": "document-synthetic", "headRevisionId": "revision-old"},
        "promptAnnotationIds": ["annotation-synthetic"],
    }
    fingerprint = request_fingerprint(params)
    if outcome == "spaced":
        params.pop("documentContext")
        params.pop("promptAnnotationIds")
        params["document_context"] = {
            "document_id": " document-synthetic ", "head_revision_id": " revision-old "
        }
        params["prompt_annotation_ids"] = [" annotation-synthetic "]
    if outcome == "no_request_id":
        params.pop("clientRequestId")
    acceptance = TurnAcceptanceResult(
        TurnIngressReceipt(
            source_scope="web:web:operator",
            request_session_key=key,
            client_request_id="request-synthetic",
            request_fingerprint="different" if outcome == "changed" else fingerprint,
            accepted_session_key=key,
            session_id="session-synthetic",
            message_id="message-synthetic",
            task_id="turn-synthetic",
        ),
        replayed=True,
        fresh_user_session=False,
    )
    lookup = AsyncMock(return_value=None if outcome == "missing" else acceptance)
    projection = AsyncMock(return_value={"status": "accepted", "replayed": True})
    # These ports deliberately omit session creation, input normalization,
    # route preparation and task activation: receipt replay cannot use them.
    ports = SimpleNamespace(
        storage=SimpleNamespace(
            capabilities=AdmissionStorageCapabilities(
                receipts=True, meta_controls=False, atomic_acceptance=False
            ),
            replay_turn_ingress_receipt=lookup,
        ),
        explicit_ingress_intent=lambda _key: nullcontext(),
        accepted_response=projection,
    )
    adapter = GatewayTurnAdmissionAdapter(DurableTurnAdmission(ports))
    if outcome in {"found", "spaced"}:
        result = await adapter.admit(params, surface=surface)
        assert result["replayed"] is True
        projection.assert_awaited_once()
    else:
        from opensquilla.gateway.rpc import RpcHandlerError

        with pytest.raises(RpcHandlerError) as caught:
            await adapter.admit(params, surface=surface)
        expected = "IDEMPOTENCY_CONFLICT" if outcome == "changed" else "DOCUMENT_EDITING_RETIRED"
        assert caught.value.code == expected
        projection.assert_not_awaited()
    if outcome == "no_request_id":
        lookup.assert_not_awaited()
    else:
        assert lookup.await_args.kwargs == {
            "source_scope": "web:webchat:operator" if surface == "webchat" else "web:web:operator",
            "request_session_key": key,
            "client_request_id": "request-synthetic",
        }


@pytest.mark.parametrize("surface", ["webchat", "session"])
@pytest.mark.parametrize("snake", [False, True])
async def test_initial_model_pin_is_normalized_at_admission_boundary(surface, snake):
    application = SimpleNamespace(admit=AsyncMock(return_value={"accepted": True}))
    params = {
        "sessionKey" if surface == "webchat" else "key": "agent:main:pin",
        "message": "hello",
        "intent": "new_chat",
        "initial_model" if snake else "initialModel": " synthetic/model ",
        "initial_provider" if snake else "initialProvider": " OpenAI ",
    }
    await GatewayTurnAdmissionAdapter(application).admit(params, surface=surface)
    command = application.admit.await_args.args[0]
    assert command.initial_model == "synthetic/model"
    assert command.initial_provider == "openai"


@pytest.mark.parametrize("fields", [
    {"initialModel": ""},
    {"initialModel": "  "},
    {"initialModel": 42},
    {"initialModel": "x" * 513},
    {"initialProvider": "openai"},
    {"initialModel": "a", "initialProvider": 42},
    {"initialModel": "a", "initialProvider": " "},
    {"initialModel": "a", "initial_model": "b"},
    {"initialModel": "a", "initialProvider": "openai", "initial_provider": "anthropic"},
])
async def test_invalid_initial_model_pin_never_reaches_application(fields):
    application = SimpleNamespace(admit=AsyncMock())
    with pytest.raises(ValueError):
        await GatewayTurnAdmissionAdapter(application).admit({
            "sessionKey": "agent:main:pin", "message": "hello", "intent": "new_chat", **fields,
        }, surface="webchat")
    application.admit.assert_not_awaited()


@pytest.mark.parametrize("with_workspace_file", [False, True])
def test_initial_model_identity_normalizes_aliases_but_preserves_provider_and_model(
    with_workspace_file,
):
    base = {"key": "agent:main:pin", "message": "hello", "intent": "new_chat"}
    workspace_file = {
        "workspaceId": "project-one", "relativePath": "notes.txt",
        "name": "notes.txt", "mime": "text/plain",
    }
    if with_workspace_file:
        base["workspaceFiles"] = [workspace_file]
    original = decode_admit_turn({**base, "initialModel": "a", "initialProvider": "openai"})
    alias = decode_admit_turn({**base, "initial_model": " a ", "initial_provider": " OpenAI "})
    assert original.request_fingerprint == alias.request_fingerprint
    for fields in ({"initialModel": "b", "initialProvider": "openai"},
                   {"initialModel": "a", "initialProvider": "anthropic"}, {}):
        changed = decode_admit_turn({**base, **fields})
        assert changed.request_fingerprint != original.request_fingerprint
    if with_workspace_file:
        duplicate = decode_admit_turn({
            **base, "initial_model": " a ", "initial_provider": " OpenAI ",
            "workspaceFiles": [workspace_file, dict(workspace_file)],
        })
        assert duplicate.request_fingerprint == original.request_fingerprint
        assert duplicate.workspace_files == (workspace_file,)
        changed_file = decode_admit_turn({
            **base, "initialModel": "a", "initialProvider": "openai",
            "workspaceFiles": [{**workspace_file, "relativePath": "changed.txt"}],
        })
        assert changed_file.request_fingerprint != original.request_fingerprint
    assert decode_admit_turn(base).request_fingerprint == decode_admit_turn({
        **base, "initialModel": None, "initialProvider": None,
    }).request_fingerprint
