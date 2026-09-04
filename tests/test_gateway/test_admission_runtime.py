from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from opensquilla.application.admission_errors import (
    AdmissionQueueFullError,
    AdmissionShuttingDownError,
)
from opensquilla.gateway.admission_input import decode_admit_turn
from opensquilla.gateway.admission_runtime import GatewayAdmissionRuntime
from opensquilla.gateway.attachment_ingest import AttachmentResolutionError
from opensquilla.gateway.rpc import RpcHandlerError
from opensquilla.gateway.task_runtime import TaskQueueFullError, TaskRuntimeShuttingDownError
from opensquilla.session.models import SessionIntent


def _ports(tmp_path, **overrides):
    return GatewayAdmissionRuntime(
        config=SimpleNamespace(
            attachments=SimpleNamespace(
                transcript_disk_budget_bytes=1234,
                opaque_max_bytes=4321,
                accept_opaque=False,
                media_root=str(tmp_path),
            ),
        ),
        manager=overrides.get("manager", SimpleNamespace(storage=object())),
        runtime=None,
        runner=None,
        is_owner=True,
        host_execute_allowed=True,
        publish=AsyncMock(),
        normalize_terminal=lambda name, payload: payload,
        session_model=lambda session, agent: None,
    )


def test_admission_normalization_preserves_frozen_legacy_source_classification(tmp_path):
    command = decode_admit_turn(
        {
            "key": "agent:main:webchat:primitive",
            "message": "x" * 20001,
            "_source": {"caller_kind": None, "callerKind": "web", "channel_kind": "cli"},
        },
        surface="session",
        principal_role="operator",
        connection_id="connection",
    )
    assert command.source.is_web is True
    assert _ports(tmp_path).normalize_input(command).metadata["guard_action"] == (
        "generated_text_attachment"
    )


@pytest.mark.parametrize("operation", ["reserve_turn", "start_turn"])
@pytest.mark.parametrize("failure_kind", ["queue", "shutdown"])
async def test_runtime_rejection_keeps_typed_identity_for_application_rollback(
    tmp_path, monkeypatch, operation, failure_kind
):
    key = "agent:main:webchat:primitive"
    failure = (
        TaskQueueFullError(session_key=key, max_pending=3)
        if failure_kind == "queue"
        else TaskRuntimeShuttingDownError(session_key=key)
    )
    helper = AsyncMock(side_effect=failure)
    monkeypatch.setattr(f"opensquilla.gateway.admission_runtime.{operation}_via_runtime", helper)
    ports = _ports(tmp_path)
    expected = AdmissionQueueFullError if failure_kind == "queue" else AdmissionShuttingDownError
    with pytest.raises(expected) as caught:
        await getattr(ports, operation)(
            object(),
            object(),
            "hello",
            attachments=[],
            mode="followup",
            run_kind="session_turn",
            no_memory_capture=False,
            semantic_message="hello",
            turn_id="turn-one",
            accepted_run_mode_override=None,
        )
    assert caught.value.session_key == key
    assert caught.value.__cause__ is failure
    if failure_kind == "queue":
        assert caught.value.max_pending == 3
    assert helper.await_count == 1


async def test_expired_upload_keeps_reupload_classification_without_consumption(
    tmp_path, monkeypatch
):
    failure = AttachmentResolutionError(
        "staged input expired",
        code="ATTACHMENT_EXPIRED",
        attachment_index=2,
        file_uuid="synthetic-upload",
        recoverable=True,
    )
    ingest = AsyncMock(side_effect=failure)
    monkeypatch.setattr(
        "opensquilla.gateway.admission_runtime.attachment_ingest.ingest_attachments", ingest
    )
    with pytest.raises(RpcHandlerError) as caught:
        await _ports(tmp_path).ingest_attachments(
            "hello", [], session_id="session-one", allow_material_refs=True
        )
    assert caught.value.code == "ATTACHMENT_EXPIRED"
    assert caught.value.retryable is True
    assert caught.value.details == {
        "attachmentIndex": 2,
        "fileUuid": "synthetic-upload",
        "recovery": "reupload",
    }
    assert ingest.await_args.kwargs["expected_material_scope"] == "session-one"
    assert ingest.await_args.kwargs["allow_material_refs"] is True
    assert ingest.await_args.kwargs["disk_budget_bytes"] == 1234
    assert ingest.await_args.kwargs["opaque_limit_bytes"] == 4321
    assert ingest.await_args.kwargs["accept_opaque"] is False


async def test_prepared_session_config_freezes_before_activation(tmp_path, monkeypatch):
    capture = Mock(return_value="frozen-config")
    monkeypatch.setattr(
        "opensquilla.gateway.admission_runtime.capture_prepared_session_model_routing_config",
        capture,
    )
    runtime = SimpleNamespace(freeze_acceptance=AsyncMock())
    reservation, session = object(), object()
    ports = _ports(tmp_path)
    await ports.freeze_acceptance(runtime, reservation, session_node=session)
    assert capture.call_args.args[1] is session
    runtime.freeze_acceptance.assert_awaited_once_with(reservation, accepted_config="frozen-config")
    runtime.freeze_acceptance.reset_mock()
    await ports.freeze_acceptance(runtime, reservation)
    runtime.freeze_acceptance.assert_awaited_once_with(reservation)


@pytest.mark.parametrize("storage_attribute", ["storage", "_storage"])
async def test_direct_turn_uses_prepared_authority_and_existing_runner(
    tmp_path, monkeypatch, storage_attribute
):
    run = AsyncMock()
    monkeypatch.setattr("opensquilla.gateway.admission_runtime.run_direct_turn", run)
    storage = object()
    ports = _ports(tmp_path, manager=SimpleNamespace(**{storage_attribute: storage}))
    route = SimpleNamespace(session_key="agent:main:webchat:primitive")
    prepared = SimpleNamespace(
        envelope=route,
        agent_id="main",
        turn_id="turn-one",
        guest_profile=object(),
        accepted_run_mode_override=object(),
        configured_workspace_dir="workspace",
        host_execute_allowed=False,
    )
    await ports.run_direct_turn(
        prepared,
        route_envelope=route,
        session_id="session-one",
        provider_message="provider",
        semantic_message="semantic",
        attachments=[],
        session_intent="continue",
        run_kind="session_turn",
        no_memory_capture=True,
        fresh_user_session=True,
        user_message_id="message-one",
        turn_context={"turn_id": "turn-one"},
    )
    assert run.await_count == 1
    assert run.await_args.kwargs["storage"] is storage
    assert run.await_args.kwargs["route_envelope"] is route
    assert run.await_args.kwargs["guest_profile"] is prepared.guest_profile
    assert (
        run.await_args.kwargs["accepted_run_mode_override"] is prepared.accepted_run_mode_override
    )
    assert run.await_args.kwargs["host_execute_allowed"] is False
    assert run.await_args.kwargs["session_key"] == route.session_key
    assert run.await_args.kwargs["turn_id"] == "turn-one"
    assert run.await_args.kwargs["no_memory_capture"] is True
    assert run.await_args.kwargs["session_intent"] is SessionIntent.CONTINUE


@pytest.mark.parametrize(
    ("message", "semantic_message", "kind", "name", "correlation"),
    [
        ("/meta sample -- request", "/meta sample -- request", "manual", "sample", "request:one"),
        (
            "/meta-replay " + "a" * 32,
            "/meta-replay " + "a" * 32,
            "replay",
            None,
            "nonce:" + "a" * 32,
        ),
        ("/meta sample", "/meta other", None, None, None),
        ("/meta-replay invalid", "/meta sample", None, None, None),
    ],
)
def test_meta_control_projection_preserves_exact_native_grammar(
    tmp_path, message, semantic_message, kind, name, correlation
):
    result = _ports(tmp_path).parse_meta_control(
        message, semantic_message, client_request_id="one"
    )
    if kind is None:
        assert result is None
    else:
        assert result.kind == kind
        assert result.name == name
        assert result.correlation_id == correlation


def test_meta_marker_primitives_preserve_identity_rollback_and_consumption(tmp_path, monkeypatch):
    from opensquilla.engine.steps import meta_command

    monkeypatch.setattr(meta_command, "_pending_meta_launch", {})
    monkeypatch.setattr(meta_command, "_consumed_meta_launch", {})
    ports = _ports(tmp_path)
    key = "agent:main:webchat:marker-primitives"
    request = {"client_request_id": "one"}
    assert meta_command.pending_meta_launch_put(key, "sample", **request) == "stamped"
    assert meta_command.pending_meta_launch_put(key, "other", client_request_id="two") == "stamped"
    assert ports.peek_meta_launch(key, **request) == "sample"
    assert ports.peek_meta_launch(key, client_request_id="missing") is None
    assert ports.cancel_accepted_meta_launch(key, **request) is False
    assert ports.promote_meta_launch(
        key, message="ordinary", semantic_message="ordinary", **request
    ) is None
    launch = {"message": "/meta sample", "semantic_message": "/meta sample"}
    assert ports.promote_meta_launch(key, **launch, **request) == "promoted"
    assert ports.promote_meta_launch(key, **launch, **request) == "accepted"
    assert ports.restage_meta_launch(key, **request) is True
    assert ports.restage_meta_launch(key, **request) is False
    assert ports.promote_meta_launch(key, **launch, **request) == "promoted"
    assert ports.cancel_accepted_meta_launch(key, **request) is True
    assert ports.cancel_accepted_meta_launch(key, **request) is False
    assert ports.peek_meta_launch(key, **request) is None
    assert meta_command.pending_meta_launch_put(key, "sample", **request) == "replayed"
    assert ports.peek_meta_launch(key, client_request_id="two") == "other"


async def test_collect_admission_releases_native_guard_before_rejection_mapping(tmp_path):
    key = "agent:main:webchat:primitive"
    events = []

    @asynccontextmanager
    async def guard(session_key):
        assert session_key == key
        events.append("enter")
        try:
            yield
        finally:
            events.append("release")

    runtime = SimpleNamespace(collect_admission=guard)
    with pytest.raises(AdmissionShuttingDownError) as caught:
        async with _ports(tmp_path).collect_admission(runtime, key):
            events.append("body")
            raise TaskRuntimeShuttingDownError(session_key=key)
    assert caught.value.session_key == key
    assert events == ["enter", "body", "release"]


async def test_collect_reuses_the_application_persistence_once_and_keeps_native_errors(tmp_path):
    handle = object()
    acceptance = object()
    persist = AsyncMock(return_value=acceptance)

    async def collect(**kwargs):
        assert kwargs["persist"] is persist
        assert kwargs["persisted_user_message_id"] == "message-one"
        return handle, await kwargs["persist"](handle, {"collected": True})

    runtime = SimpleNamespace(try_collect_atomically=collect)
    ports = _ports(tmp_path)
    arguments = {
        "envelope": object(),
        "message": "hello",
        "attachments": [],
        "run_kind": "session_turn",
        "no_memory_capture": True,
        "semantic_message": "hello",
        "persisted_user_message_id": "message-one",
        "message_count": 1,
        "accepted_run_mode_override": None,
        "persist": persist,
    }
    assert await ports.try_collect_atomically(runtime, **arguments) == (handle, acceptance)
    persist.assert_awaited_once_with(handle, {"collected": True})

    failure = RuntimeError("durable commit failed")
    runtime.try_collect_atomically = AsyncMock(side_effect=failure)
    with pytest.raises(RuntimeError) as caught:
        await ports.try_collect_atomically(runtime, **arguments)
    assert caught.value is failure
    persist.assert_awaited_once()
