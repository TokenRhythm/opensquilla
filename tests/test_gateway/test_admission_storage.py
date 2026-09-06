"""Native admission primitive identity, capabilities, and transaction mapping."""

from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opensquilla.application.admission_views import AdmissionCommit, AdmissionProjectOrigin
from opensquilla.gateway.admission_storage import GatewayAdmissionSessions, GatewayAdmissionStorage
from opensquilla.project_workspaces import ProjectWorkspaceStateError, project_path_key
from opensquilla.run_mode import RunMode
from opensquilla.sandbox.run_context import RUN_CONTEXT_ORIGIN_KEY
from opensquilla.session.goals import ClaimCurrentGoalMutation
from opensquilla.session.manager import PreparedSessionIntent
from opensquilla.session.models import (
    ProjectWorkspace,
    SessionIntent,
    SessionNode,
    TranscriptEntry,
    TurnIngressReceipt,
)
from opensquilla.session.storage import TurnAcceptanceResult
from opensquilla.session.turn_context import current_turn_context


def _acceptance():
    return TurnAcceptanceResult(
        receipt=TurnIngressReceipt(
            source_scope="web:owner",
            request_session_key="agent:main:webchat:demo",
            client_request_id="request-demo",
            request_fingerprint="fingerprint-demo",
            accepted_session_key="agent:main:webchat:demo",
            session_id="session-demo",
            message_id="message-demo",
            task_id="task-demo",
        ),
        replayed=False,
        fresh_user_session=False,
    )


@pytest.mark.asyncio
async def test_commit_maps_every_field_once_without_copying_native_material():
    native = SimpleNamespace(accept_turn=AsyncMock(return_value=_acceptance()))
    port = GatewayAdmissionStorage(native)
    entry = TranscriptEntry(
        message_id="message-demo",
        session_id="session-demo",
        session_key="agent:main:webchat:demo",
        role="user",
        content="synthetic input",
    )
    # Opaque native rows stay identical through the fixed primitive; the native
    # transaction remains responsible for their validation and serialization.
    node = SessionNode(session_key="agent:main:webchat:demo", session_id="session-demo")
    task = SimpleNamespace(task_id="task-demo")
    revision = SimpleNamespace(revision_id="revision-demo")
    run = SimpleNamespace(run_id="run-demo")
    target = SimpleNamespace(expected_annotation=SimpleNamespace(annotation_id="annotation-demo"))
    guard = object()
    archive_writer = AsyncMock()
    command = AdmissionCommit(
        entry=entry,
        expected_epoch=4,
        updated_at=12345,
        task_record=task,
        source_scope="web:owner",
        request_session_key=node.session_key,
        client_request_id="request-demo",
        request_fingerprint="fingerprint-demo",
        session_node=node,
        reset_from_session_id="previous-session",
        reset_archive_writer=archive_writer,
        initial_transcript_entries=(entry,),
        session_updates={"collaboration_mode": "default", "origin": {"synthetic": True}},
        plan_revision=revision,
        plan_run=run,
        merge_into_task=True,
        meta_control_intent_id="meta-demo",
        workspace_guard=guard,
        expected_collaboration_revision=8,
        expected_active_plan_revision_id="active-demo",
        require_idle_for_current_plan_implementation=True,
        claim_current_goal=True,
        prepared_prompt_annotation_targets=(target,),
        prompt_annotation_turn_id="turn-demo",
        pending_input_id="pending-demo",
        pending_input_fingerprint="pending-fingerprint",
        pending_input_revision=9,
    )
    result = await port.accept_turn(command)
    assert result is native.accept_turn.return_value
    native.accept_turn.assert_awaited_once()
    call = native.accept_turn.await_args
    assert call.args == (entry,)
    assert set(call.kwargs) == {
        field.name for field in fields(command) if field.name not in {"entry", "claim_current_goal"}
    } | {"goal_mutation"}
    for field in fields(command):
        if field.name not in {"entry", "claim_current_goal"}:
            assert call.kwargs[field.name] is getattr(command, field.name)
    assert isinstance(call.kwargs["goal_mutation"], ClaimCurrentGoalMutation)


@pytest.mark.asyncio
async def test_commit_defaults_do_not_create_goal_or_optional_mutation():
    native = SimpleNamespace(accept_turn=AsyncMock(return_value=_acceptance()))
    command = AdmissionCommit(
        entry=TranscriptEntry(session_id="session-demo", role="user", content="synthetic"),
        expected_epoch=0,
        updated_at=1,
        task_record=None,
        source_scope="web:owner",
        request_session_key="agent:main:webchat:demo",
        client_request_id="request-demo",
        request_fingerprint="fingerprint-demo",
    )
    await GatewayAdmissionStorage(native).accept_turn(command)
    args = native.accept_turn.await_args.kwargs
    assert args["goal_mutation"] is None
    assert args["reset_archive_writer"] is None
    assert args["initial_transcript_entries"] == ()
    assert args["prepared_prompt_annotation_targets"] == ()
    assert args["require_idle_for_current_plan_implementation"] is False
    assert args["merge_into_task"] is False
    assert args["pending_input_revision"] is None


@pytest.mark.asyncio
async def test_legacy_capabilities_and_message_context_do_not_require_modern_manager():
    node = SimpleNamespace(
        session_key="agent:main:webchat:demo", session_id="session-demo", agent_id="main"
    )
    observed = []

    async def append(key, *, role, content):
        observed.append((key, current_turn_context()))
        return SimpleNamespace(message_id="message-demo", content=content)

    native = SimpleNamespace(
        get_or_create=AsyncMock(return_value=node),
        append_message=append,
        get_transcript=AsyncMock(return_value=[SimpleNamespace(role="user", content="previous")]),
    )
    port = GatewayAdmissionSessions(native)
    assert not port.capabilities.prepared_intent
    assert not port.capabilities.prepared_message
    assert not port.capabilities.apply_intent
    assert not port.capabilities.archive
    assert (
        await port.get_or_create(session_key=node.session_key, agent_id="main", display_name="demo")
        is node
    )
    context = {"turn_id": "turn-demo"}
    entry = await port.append_message(
        node.session_key, role="user", content="synthetic", turn_context=context
    )
    assert entry.content == "synthetic"
    assert observed == [(node.session_key, context)]
    assert current_turn_context() is None
    assert await port.has_transcript(node.session_key) is True
    native.get_transcript.return_value = []
    assert await port.has_transcript(node.session_key) is False
    assert not GatewayAdmissionStorage(SimpleNamespace()).capabilities.atomic_acceptance


@pytest.mark.asyncio
async def test_modern_intent_keeps_enum_origin_and_native_plan_identity():
    node = SessionNode(session_key="agent:main:webchat:demo", session_id="session-demo")
    plan = PreparedSessionIntent(node=node, action="reset", expected_epoch=3)
    native = SimpleNamespace(
        prepare_intent=AsyncMock(return_value=plan), prepare_message=AsyncMock()
    )
    port = GatewayAdmissionSessions(native)
    assert port.capabilities.prepared_intent
    assert port.capabilities.prepared_message
    result = await port.prepare_intent(
        node.session_key,
        "reset_same_key",
        agent_id="main",
        origin=AdmissionProjectOrigin(RunMode.SAFE, "/synthetic/workspace", "config"),
    )
    assert result is plan
    call = native.prepare_intent.await_args
    assert call.args[1] is SessionIntent.RESET_SAME_KEY
    assert call.kwargs["origin"][RUN_CONTEXT_ORIGIN_KEY] == {
        "run_mode": "safe",
        "run_mode_source": "config",
        "workspace": "/synthetic/workspace",
        "mounts": [],
        "domains": [],
        "bundles": [],
        "public_network": [],
        "temporary_grants": [],
    }
    assert "display_name" not in call.kwargs


@pytest.mark.asyncio
async def test_workspace_selection_uses_native_store_and_keeps_validation(tmp_path):
    path = str(tmp_path.resolve())
    workspace = ProjectWorkspace(
        workspace_id="workspace-demo",
        display_name="Demo",
        path=path,
        path_key=project_path_key(path),
        trusted_at=1,
    )
    native = SimpleNamespace(get_project_workspace=AsyncMock(return_value=workspace))
    port = GatewayAdmissionStorage(native)
    selected = await port.resolve_workspace("workspace-demo")
    assert selected.workspace is workspace
    assert selected.guard.workspace_id == "workspace-demo"
    assert selected.guard.path == path
    native.get_project_workspace.assert_awaited_once_with("workspace-demo")
    native.get_project_workspace.return_value = None
    with pytest.raises(ProjectWorkspaceStateError) as error:
        await port.resolve_workspace("missing-workspace")
    assert error.value.reason == "not_found"
