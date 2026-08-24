"""End-to-end RPC contracts for durable, atomic turn acceptance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    AnchorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
    PromptAnnotationStatus,
)
from opensquilla.artifacts import ArtifactStore
from opensquilla.attachment_refs import (
    PENDING_CHAT_INPUT_MATERIAL_STORE,
    pending_chat_input_material_path,
    transcript_material_path,
)
from opensquilla.engine.steps.meta_command import (
    format_meta_replay_sentinel,
    meta_command_launch,
    pending_meta_launch_peek,
    pending_meta_launch_pop,
    pending_meta_launch_put,
    pending_meta_launch_state,
)
from opensquilla.gateway.agent_tasks import get_agent_task_registry
from opensquilla.gateway.artifact_contexts import (
    DOCUMENT_CONTEXT_TOOL_NAMES,
    PROMPT_ANNOTATION_SOURCE_TOOL_NAMES,
    PROMPT_ANNOTATION_TOOL_NAMES,
    BoundDocumentContext,
)
from opensquilla.gateway.auth import Principal
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.model_routing import (
    capture_model_routing_config,
    model_routing_snapshot,
)
from opensquilla.gateway.routing import RouteEnvelope, SourceKind
from opensquilla.gateway.rpc import RpcContext, get_dispatcher
from opensquilla.gateway.rpc_sessions import _handle_sessions_send
from opensquilla.gateway.session_model_routing import (
    capture_accepted_model_routing_config,
)
from opensquilla.gateway.task_runtime import TaskRuntime
from opensquilla.gateway.turn_ingress import request_fingerprint
from opensquilla.gateway.uploads import UploadStore, get_upload_store, set_upload_store
from opensquilla.session.goals import GoalCommandRequest, StartGoalMutation, new_goal
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import (
    AgentTaskRecord,
    AgentTaskStatus,
    TranscriptEntry,
    TurnIngressReceipt,
)
from opensquilla.session.storage import SessionStorage, TurnAcceptanceResult
from opensquilla.session.turn_context import current_turn_context, turn_context_scope

SESSION_KEY = "agent:main:webchat:atomic-ingress"
CLIENT_REQUEST_ID = "client-request-atomic-1"

_PRINCIPAL = Principal(
    role="operator",
    scopes=frozenset(["operator.admin"]),
    is_owner=True,
    authenticated=True,
)


@dataclass
class _RealIngressStack:
    db_path: Path
    storage: SessionStorage
    manager: SessionManager
    runtime: TaskRuntime
    context: RpcContext
    session_id: str
    handler_started: asyncio.Event
    release_handler: asyncio.Event

    async def wait_until_running(self) -> None:
        await asyncio.wait_for(self.handler_started.wait(), timeout=2.0)


@asynccontextmanager
async def _open_real_stack(
    db_path: Path,
    *,
    max_pending_per_session: int = 64,
) -> AsyncIterator[_RealIngressStack]:
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage, inject_time_prefix=False)
    session = await manager.create(
        SESSION_KEY,
        agent_id="main",
        display_name="Atomic ingress test",
    )
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()

    async def _turn_handler(_run: Any) -> None:
        handler_started.set()
        await release_handler.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_turn_handler,
        max_concurrency=1,
        max_pending_per_session=max_pending_per_session,
        running_heartbeat_interval_s=None,
    )
    context = RpcContext(
        conn_id="atomic-ingress-test",
        principal=_PRINCIPAL,
        config=GatewayConfig(
            workspace_dir=str(db_path.parent / "workspace"),
            attachments={"media_root": str(db_path.parent / "media")},
            memory={"flush_enabled": False},
            naming={"enabled": False},
        ),
        session_manager=manager,
        task_runtime=runtime,
    )
    stack = _RealIngressStack(
        db_path=db_path,
        storage=storage,
        manager=manager,
        runtime=runtime,
        context=context,
        session_id=session.session_id,
        handler_started=handler_started,
        release_handler=release_handler,
    )
    try:
        yield stack
    finally:
        release_handler.set()
        for reservations in list(runtime._reservations_by_session.values()):
            for reservation in list(reservations):
                await runtime.abort_reservation(reservation)
        await runtime.shutdown(cancel=True, timeout=2.0)
        await storage.close()


def _table_counts(db_path: Path) -> dict[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in (
                "transcript_entries",
                "agent_tasks",
                "turn_ingress_receipts",
            )
        }
    finally:
        connection.close()


def _assert_no_runtime_acceptance_state(runtime: TaskRuntime) -> None:
    assert runtime._reservations_by_session == {}
    assert runtime._tasks == {}
    assert runtime._pending_by_session == {}
    assert runtime._running_by_session == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "run_kind_injection"),
    [
        ("sessions.send", {"runKind": "cron_turn"}),
        ("chat.send", {"run_kind": "cron_turn"}),
        ("sessions.send", {"_source": {"run_kind": "cron_turn"}}),
    ],
)
async def test_public_send_cannot_inject_global_routing_run_kind(
    tmp_path: Path,
    method: str,
    run_kind_injection: dict[str, Any],
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        global_config = GatewayConfig(
            squilla_router={"enabled": True, "rollout_phase": "full"},
        )

        async def accepted_config_provider(*, session_key: str, run_kind: str) -> Any:
            return await capture_accepted_model_routing_config(
                global_config,
                stack.manager,
                session_key=session_key,
                run_kind=run_kind,
            )

        stack.runtime._accepted_config_provider = accepted_config_provider
        params: dict[str, Any] = {
            "message": "ordinary public input",
            "clientRequestId": f"public-run-kind-{method}",
            **run_kind_injection,
        }
        if method == "chat.send":
            params["sessionKey"] = SESSION_KEY
        else:
            params["key"] = SESSION_KEY

        response = await get_dispatcher().dispatch(
            f"rpc-public-run-kind-{method}",
            method,
            params,
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None and task.details is not None
        audit = task.details["accepted_model_routing"]
        assert task.run_kind == "session_turn"
        assert audit["run_kind"] == "session_turn"
        assert audit["scope"] == "session"
        assert audit["effective_mode"] == "direct"
        assert model_routing_snapshot(global_config)["mode"] == "router"


@pytest.mark.asyncio
async def test_internal_send_can_supply_trusted_background_run_kind(tmp_path: Path) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        global_config = GatewayConfig(
            squilla_router={"enabled": True, "rollout_phase": "full"},
        )

        async def accepted_config_provider(*, session_key: str, run_kind: str) -> Any:
            return await capture_accepted_model_routing_config(
                global_config,
                stack.manager,
                session_key=session_key,
                run_kind=run_kind,
            )

        stack.runtime._accepted_config_provider = accepted_config_provider
        accepted = await _handle_sessions_send(
            {
                "key": SESSION_KEY,
                "message": "trusted internal background input",
                "clientRequestId": "trusted-internal-run-kind",
            },
            stack.context,
            trusted_run_kind="cron_turn",
        )
        await stack.wait_until_running()

        task = await stack.storage.get_agent_task(accepted["task_id"])
        assert task is not None and task.details is not None
        audit = task.details["accepted_model_routing"]
        assert task.run_kind == "cron_turn"
        assert audit["run_kind"] == "cron_turn"
        assert audit["scope"] == "global"
        assert audit["effective_mode"] == "router"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "display_text"),
    [("/coding", "//coding"), ("//usr/bin/env", "///usr/bin/env")],
)
async def test_pending_input_literal_slash_escape_dispatches_normalized_message(
    tmp_path: Path,
    message: str,
    display_text: str,
) -> None:
    async with _open_real_stack(tmp_path / "pending-literal-slash.db") as stack:
        staged = await get_dispatcher().dispatch(
            "pending-literal-slash-enqueue",
            "sessions.pending_inputs.enqueue",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-literal-slash",
                "clientRequestId": "pending-literal-slash-request",
                "clientMessageId": "pending-literal-slash-message",
                "message": message,
                "displayText": display_text,
            },
            stack.context,
        )
        assert staged.ok is True
        row = await stack.storage.get_pending_chat_input("pending-literal-slash")
        assert row is not None
        assert row.payload["message"] == message
        assert row.payload["displayText"] == display_text

        listed = await get_dispatcher().dispatch(
            "pending-literal-slash-list",
            "sessions.pending_inputs.list",
            {"key": SESSION_KEY},
            stack.context,
        )
        assert listed.ok is True
        assert listed.payload["items"][0]["message"] == message
        assert listed.payload["items"][0]["displayText"] == display_text

        accepted = await get_dispatcher().dispatch(
            "pending-literal-slash-dispatch",
            "sessions.pending_inputs.dispatch",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-literal-slash",
                "clientRequestId": "pending-literal-slash-request",
                "requestFingerprint": staged.payload["requestFingerprint"],
            },
            stack.context,
        )
        assert accepted.ok is True
        transcript = await stack.storage.get_transcript(stack.session_id)
        entry = next(
            item
            for item in transcript
            if item.message_id == accepted.payload["message_id"]
        )
        persisted_content = json.loads(entry.content)
        assert persisted_content["text"] == message
        assert persisted_content["display_text"] == display_text


@pytest.mark.asyncio
async def test_pending_input_confirmed_plain_slash_survives_staging_and_dispatch(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "pending-confirmed-plain.db") as stack:
        staged = await get_dispatcher().dispatch(
            "pending-confirmed-plain-enqueue",
            "sessions.pending_inputs.enqueue",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-confirmed-plain",
                "clientRequestId": "pending-confirmed-plain-request",
                "clientMessageId": "pending-confirmed-plain-message",
                "message": "/gamemode creative",
                "confirmedPlainText": True,
            },
            stack.context,
        )
        assert staged.ok is True
        row = await stack.storage.get_pending_chat_input("pending-confirmed-plain")
        assert row is not None
        assert row.payload["message"] == "/gamemode creative"
        assert row.payload["confirmedPlainText"] is True

        listed = await get_dispatcher().dispatch(
            "pending-confirmed-plain-list",
            "sessions.pending_inputs.list",
            {"key": SESSION_KEY},
            stack.context,
        )
        assert listed.ok is True
        assert listed.payload["items"][0]["confirmedPlainText"] is True

        accepted = await get_dispatcher().dispatch(
            "pending-confirmed-plain-dispatch",
            "sessions.pending_inputs.dispatch",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-confirmed-plain",
                "clientRequestId": "pending-confirmed-plain-request",
                "requestFingerprint": staged.payload["requestFingerprint"],
            },
            stack.context,
        )
        assert accepted.ok is True
        transcript = await stack.storage.get_transcript(stack.session_id)
        entry = next(
            item
            for item in transcript
            if item.message_id == accepted.payload["message_id"]
        )
        assert entry.content == "/gamemode creative"


@pytest.mark.asyncio
@pytest.mark.parametrize("display_text", [None, "//different"])
async def test_pending_input_rejects_unmarked_control_commands(
    tmp_path: Path,
    display_text: str | None,
) -> None:
    suffix = "missing" if display_text is None else "mismatched"
    async with _open_real_stack(tmp_path / f"pending-control-{suffix}.db") as stack:
        params = {
            "key": SESSION_KEY,
            "pendingInputId": "pending-control",
            "clientRequestId": "pending-control-request",
            "clientMessageId": "pending-control-message",
            "message": "/coding",
        }
        if display_text is not None:
            params["displayText"] = display_text
        rejected = await get_dispatcher().dispatch(
            "pending-control-enqueue",
            "sessions.pending_inputs.enqueue",
            params,
            stack.context,
        )
        assert rejected.ok is False
        assert rejected.error is not None
        assert rejected.error.code == "PENDING_CONTROL_COMMAND_UNSUPPORTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    ["/coding", "/reset now", "/clear now", "/plan draft"],
)
async def test_pending_input_plain_marker_rejects_registered_web_controls(
    tmp_path: Path,
    message: str,
) -> None:
    suffix = message.split(maxsplit=1)[0].removeprefix("/")
    async with _open_real_stack(tmp_path / f"pending-registered-{suffix}.db") as stack:
        rejected = await get_dispatcher().dispatch(
            "pending-registered-enqueue",
            "sessions.pending_inputs.enqueue",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-registered",
                "clientRequestId": "pending-registered-request",
                "clientMessageId": "pending-registered-message",
                "message": message,
                "confirmedPlainText": True,
            },
            stack.context,
        )
        assert rejected.ok is False
        assert rejected.error is not None
        assert rejected.error.code == "PENDING_CONTROL_COMMAND_UNSUPPORTED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "display_text", "confirmed_plain_text"),
    [
        ("ordinary provider text", "/reset", False),
        ("/gamemode creative", "/reset", True),
    ],
)
async def test_pending_input_rejects_unpaired_display_text(
    tmp_path: Path,
    message: str,
    display_text: str,
    confirmed_plain_text: bool,
) -> None:
    async with _open_real_stack(tmp_path / "pending-display-mismatch.db") as stack:
        rejected = await get_dispatcher().dispatch(
            "pending-display-mismatch-enqueue",
            "sessions.pending_inputs.enqueue",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-display-mismatch",
                "clientRequestId": "pending-display-mismatch-request",
                "clientMessageId": "pending-display-mismatch-message",
                "message": message,
                "displayText": display_text,
                "confirmedPlainText": confirmed_plain_text,
            },
            stack.context,
        )
        assert rejected.ok is False
        assert rejected.error is not None
        assert rejected.error.code == "PENDING_DISPLAY_TEXT_MISMATCH"


async def _seed_idle_active_goal(stack: _RealIngressStack) -> Any:
    """Create a settled active Goal without installing an execution lease."""

    session = await stack.storage.get_session(SESSION_KEY)
    assert session is not None
    task_id = "meta-control-goal-bootstrap-task"
    objective = "Keep the Goal active while a control turn runs."
    command = GoalCommandRequest(
        source_scope="web:test-owner",
        request_session_key=SESSION_KEY,
        client_request_id="00000000-0000-4000-8000-000000000001",
        action="set",
        request_fingerprint="a" * 64,
    )
    goal = new_goal(
        goal_id="meta-control-active-goal",
        session_key=SESSION_KEY,
        session_id=session.session_id,
        session_epoch=session.epoch,
        objective=objective,
        task_id=task_id,
        created_at_ms=100,
    )
    accepted = await stack.storage.accept_turn(
        TranscriptEntry(
            session_id=session.session_id,
            session_key=SESSION_KEY,
            message_id="meta-control-goal-bootstrap-message",
            role="user",
            content=objective,
            created_at=100,
        ),
        expected_epoch=session.epoch,
        updated_at=100,
        task_record=AgentTaskRecord(
            task_id=task_id,
            session_key=SESSION_KEY,
            agent_id="main",
            source_kind="webui",
            queue_mode="followup",
            run_kind="goal",
            status=AgentTaskStatus.QUEUED,
            created_at=100,
            updated_at=100,
        ),
        source_scope=command.source_scope,
        request_session_key=SESSION_KEY,
        client_request_id=command.client_request_id,
        request_fingerprint=command.request_fingerprint,
        goal_mutation=StartGoalMutation(goal=goal, command=command),
    )
    assert accepted.goal_context is not None
    await stack.storage.update_agent_task(
        task_id,
        status=AgentTaskStatus.SUCCEEDED,
        started_at=110,
        finished_at=120,
        terminal_reason="completed",
    )
    settled = await stack.storage.settle_goal_task(
        accepted.goal_context,
        max_turns=50,
        runtime_budget_seconds=3_600,
    )
    assert settled is not None
    assert settled.status == "active"
    assert settled.active_task_id is None
    return settled


async def _create_html_prompt_annotation(
    stack: _RealIngressStack,
    *,
    annotation_id: str,
) -> tuple[ArtifactSessionService, Any]:
    service = await ArtifactSessionService.from_session_storage(stack.storage)
    source = b"<html><body><h1>Original</h1></body></html>"
    ref = ArtifactStore(
        Path(stack.context.config.attachments.media_root or "")
    ).publish_bytes(
        source,
        session_id=stack.session_id,
        session_key=SESSION_KEY,
        name="page.html",
        mime="text/html",
        source="test",
    )
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=stack.session_id,
        name="page.html",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "user-1"),
    )
    anchor = await service.create_anchor(
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        kind=AnchorKind.DOM_SOURCE,
        locator={
            "start_offset": source.decode().index("<h1>"),
            "start_tag_end_offset": source.decode().index("<h1>") + len("<h1>"),
            "tag_name": "h1",
            "source_sha256": ref.sha256,
            "offset_encoding": "unicode-code-point",
        },
        quote="<h1>",
        actor=Actor(ActorKind.USER, "user-1"),
    )
    draft = await service.create_prompt_annotation(
        annotation_id=annotation_id,
        session_key=SESSION_KEY,
        session_id=stack.session_id,
        session_epoch=0,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        anchor_id=anchor.anchor_id,
        body="Change this heading to Accepted.",
    )
    return service, draft


async def _create_html_document(
    stack: _RealIngressStack,
    *,
    session_key: str = SESSION_KEY,
    session_id: str | None = None,
) -> tuple[ArtifactSessionService, Any]:
    service = await ArtifactSessionService.from_session_storage(stack.storage)
    created = await service.create_document(
        session_key=session_key,
        session_id=session_id or stack.session_id,
        name="page.html",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=f"artifact-{session_key}-{session_id or stack.session_id}",
            sha256="c" * 64,
            filename="page.html",
            media_type="text/html",
            byte_size=32,
        ),
        actor=Actor(ActorKind.USER, "user-1"),
    )
    return service, created


@pytest.mark.asyncio
async def test_chat_send_binds_current_document_head_as_additive_runtime_context(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "document-context.db") as stack:
        _service, created = await _create_html_document(stack)
        params = {
            "sessionKey": SESSION_KEY,
            "message": "Update the open document heading.",
            "clientRequestId": "document-context-ingress-1",
            "documentContext": {
                "documentId": created.document.document_id,
                "headRevisionId": created.revision.revision_id,
            },
        }

        response = await get_dispatcher().dispatch(
            "rpc-document-context-ingress",
            "chat.send",
            params,
            stack.context,
        )
        assert response.error is None, response.error
        await stack.wait_until_running()

        runtime_task = stack.runtime._tasks[response.payload["task_id"]]
        bound = runtime_task.envelope.runtime_services["artifact_context"]
        assert isinstance(bound, BoundDocumentContext)
        assert bound.document_id == created.document.document_id
        assert bound.revision_id == created.revision.revision_id
        assert bound.tool_names == DOCUMENT_CONTEXT_TOOL_NAMES
        assert "document_read" in bound.request_context_prompt
        assert "document_patch" in bound.request_context_prompt
        assert "first source read MUST be document_read" in bound.request_context_prompt
        assert 'cursor=""' in bound.request_context_prompt
        assert "provider adapter requires that field" in bound.request_context_prompt
        assert "Never invent a non-empty cursor" in bound.request_context_prompt
        assert "sha256 returned by document_read" in bound.request_context_prompt
        assert "write_file, edit_file, and apply_patch" in bound.request_context_prompt
        assert "MUST NOT substitute for document_patch" in bound.request_context_prompt
        assert "workspace mutators are unavailable" in bound.request_context_prompt

        from opensquilla.gateway.routing import tool_context_from_envelope

        tool_context = tool_context_from_envelope(
            runtime_task.envelope,
            is_owner=True,
        )
        assert tool_context.surfaced_tools == set(DOCUMENT_CONTEXT_TOOL_NAMES)
        assert tool_context.exclusive_tools is None
        assert tool_context.allowed_tools is None

        replay_params = {key: value for key, value in params.items() if key != "documentContext"}
        replay_params["document_context"] = {
            "document_id": created.document.document_id,
            "head_revision_id": created.revision.revision_id,
        }
        replay = await get_dispatcher().dispatch(
            "rpc-document-context-ingress-replay",
            "chat.send",
            replay_params,
            stack.context,
        )
        assert replay.error is None, replay.error
        assert replay.payload["replayed"] is True

        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_chat_send_carries_latest_annotation_focus_into_document_followup(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "document-context-followup.db") as stack:
        service, draft = await _create_html_prompt_annotation(
            stack,
            annotation_id="annotation-followup-1",
        )
        document = await service.get_document(draft.document_id)
        revision = await service.get_revision(draft.revision_id)
        anchor = await service.get_anchor(draft.anchor_id)
        snapshot = {
            "version": 1,
            "annotationId": draft.annotation_id,
            "order": 0,
            "body": draft.body,
            "targetText": "Original",
            "targetKind": "heading",
            "targetStatus": "ready",
            "targetReason": None,
            "document": {
                "id": document.document_id,
                "name": document.name,
                "kind": document.kind.value,
            },
            "revision": {
                "id": revision.revision_id,
                "generation": revision.generation,
                "sha256": revision.artifact_sha256,
            },
            "anchor": {
                "id": anchor.anchor_id,
                "kind": anchor.kind.value,
                "tagName": "h1",
                "locator": anchor.locator,
                "quote": anchor.quote,
            },
        }
        session = await stack.storage.get_session(SESSION_KEY)
        assert session is not None
        await stack.storage.append_transcript_entry(
            TranscriptEntry(
                session_id=stack.session_id,
                session_key=SESSION_KEY,
                message_id="annotation-followup-history-1",
                role="user",
                content=json.dumps(
                    {
                        "text": "？",
                        "attachments": [],
                        "prompt_annotations": [snapshot],
                    },
                    ensure_ascii=False,
                ),
                created_at=1,
            ),
            expected_epoch=session.epoch,
        )

        response = await get_dispatcher().dispatch(
            "rpc-document-context-followup",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "删除这个标题",
                "clientRequestId": "document-context-followup-1",
                "documentContext": {
                    "documentId": document.document_id,
                    "headRevisionId": revision.revision_id,
                },
            },
            stack.context,
        )
        assert response.error is None, response.error
        await stack.wait_until_running()

        runtime_task = stack.runtime._tasks[response.payload["task_id"]]
        bound = runtime_task.envelope.runtime_services["artifact_context"]
        assert isinstance(bound, BoundDocumentContext)
        assert "<previous_annotation_focus readonly='true'>" in bound.request_context_prompt
        assert "page.html" in bound.request_context_prompt
        assert "Original" in bound.request_context_prompt
        assert "<previous_intent>Change this heading to Accepted.</previous_intent>" in (
            bound.request_context_prompt
        )
        assert draft.annotation_id not in bound.request_context_prompt
        assert revision.revision_id not in bound.request_context_prompt
        assert revision.artifact_sha256 not in bound.request_context_prompt

        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_owner_web_turn_receives_narrow_generated_artifact_adopter(
    tmp_path: Path,
) -> None:
    from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
    from opensquilla.gateway.routing import tool_context_from_envelope

    async with _open_real_stack(tmp_path / "generated-artifact-adopter.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-generated-artifact-adopter",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "Generate a small HTML page.",
                "clientRequestId": "generated-artifact-adopter-1",
            },
            stack.context,
        )
        assert response.error is None, response.error
        await stack.wait_until_running()

        runtime_task = stack.runtime._tasks[response.payload["task_id"]]
        adopter = runtime_task.envelope.runtime_services.get(
            "generated_artifact_adopter"
        )
        assert isinstance(adopter, GeneratedArtifactAdopter)
        assert adopter.session_key == SESSION_KEY
        assert adopter.session_id == stack.session_id
        context = tool_context_from_envelope(runtime_task.envelope, is_owner=True)
        assert context.generated_artifact_adopter is adopter

        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_chat_send_rejects_unbound_document_context_before_acceptance(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "document-context-cross-session.db") as stack:
        _service, created = await _create_html_document(
            stack,
            session_key="agent:main:webchat:another-session",
            session_id="another-session-id",
        )

        response = await get_dispatcher().dispatch(
            "rpc-document-context-cross-session",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "Edit this document.",
                "clientRequestId": "document-context-cross-session",
                "documentContext": {
                    "documentId": created.document.document_id,
                    "headRevisionId": created.revision.revision_id,
                },
            },
            stack.context,
        )

        assert response.error is not None
        assert response.error.code == "DOCUMENT_UNAVAILABLE"
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        _assert_no_runtime_acceptance_state(stack.runtime)


@pytest.mark.asyncio
async def test_chat_send_treats_document_context_revision_as_freshness_hint(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "document-context-hint.db") as stack:
        _service, created = await _create_html_document(stack)

        response = await get_dispatcher().dispatch(
            "rpc-document-context-hint",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "Edit this document.",
                "clientRequestId": "document-context-hint",
                "documentContext": {
                    "documentId": created.document.document_id,
                    "headRevisionId": "old-client-head-hint",
                },
            },
            stack.context,
        )

        assert response.error is None, response.error
        await stack.wait_until_running()
        runtime_task = stack.runtime._tasks[response.payload["task_id"]]
        bound = runtime_task.envelope.runtime_services["artifact_context"]
        assert isinstance(bound, BoundDocumentContext)
        assert bound.revision_id == created.revision.revision_id
        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_chat_send_rejects_document_context_for_non_owner(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "document-context-guest.db") as stack:
        _service, created = await _create_html_document(stack)
        stack.context.principal = Principal(
            role="operator",
            scopes=frozenset({"operator.write"}),
            is_owner=False,
            authenticated=True,
        )
        response = await get_dispatcher().dispatch(
            "rpc-document-context-guest",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "Edit this document.",
                "clientRequestId": "document-context-guest",
                "documentContext": {
                    "documentId": created.document.document_id,
                    "headRevisionId": created.revision.revision_id,
                },
            },
            stack.context,
        )

        assert response.error is not None
        assert response.error.code == "DOCUMENT_CONTEXT_FORBIDDEN"
        assert response.error.accepted is False


@pytest.mark.asyncio
async def test_chat_send_atomically_consumes_prompt_annotations_into_runtime_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        stack.context.config.naming.enabled = True
        await stack.manager.update(SESSION_KEY, display_name="WebChat")
        scheduled_titles: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def _record_auto_title(*args: Any, **kwargs: Any) -> None:
            scheduled_titles.append((args, kwargs))

        monkeypatch.setattr(
            "opensquilla.gateway.rpc_sessions._schedule_auto_title",
            _record_auto_title,
        )
        service, draft = await _create_html_prompt_annotation(
            stack,
            annotation_id="annotation-ingress-1",
        )

        response = await get_dispatcher().dispatch(
            "rpc-prompt-annotation-ingress",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "",
                "clientRequestId": "prompt-annotation-ingress-1",
                "promptAnnotationIds": [draft.annotation_id],
            },
            stack.context,
        )
        assert response.error is None, response.error
        await stack.wait_until_running()

        assert response.payload["acceptedPromptAnnotationIds"] == [draft.annotation_id]
        sent = await service.get_prompt_annotation(draft.annotation_id)
        assert sent.status is PromptAnnotationStatus.SENT
        assert sent.anchor_id != draft.anchor_id
        normalized_anchor = await service.get_anchor(sent.anchor_id)
        assert normalized_anchor.remapped_from_anchor_id == draft.anchor_id
        assert sent.sent_message_id == response.payload["user_message_id"]
        assert sent.sent_turn_id == response.payload["task_id"]
        entries = await stack.storage.get_transcript(stack.session_id)
        envelope = json.loads(entries[-1].content)
        assert envelope["prompt_annotations"][0]["annotationId"] == draft.annotation_id
        assert envelope["prompt_annotations"][0]["body"] == draft.body
        assert envelope["prompt_annotations"][0]["targetStatus"] == "ready"
        assert envelope["prompt_annotations"][0]["targetKind"] == "heading"

        runtime_task = stack.runtime._tasks[response.payload["task_id"]]
        bound = runtime_task.envelope.runtime_services["artifact_context"]
        assert bound.operation_class == "selection_edit"
        assert bound.annotation_ids == (draft.annotation_id,)
        assert bound.targets[0].status == "ready"
        assert bound.targets[0].anchor_id == sent.anchor_id
        # The real-stack fixture has no Electron bridge.  It therefore uses
        # the protocol-v3 source-only compatibility surface; a live v4
        # desktop context receives the full candidate/browser loop.
        assert bound.tool_names in {
            PROMPT_ANNOTATION_TOOL_NAMES,
            PROMPT_ANNOTATION_SOURCE_TOOL_NAMES,
        }
        assert "document_apply" in bound.request_context_prompt
        assert "html_edit_source" not in bound.request_context_prompt
        assert "version=" not in bound.request_context_prompt
        assert scheduled_titles == []

        replay = await get_dispatcher().dispatch(
            "rpc-prompt-annotation-ingress-replay",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "",
                "clientRequestId": "prompt-annotation-ingress-1",
                "promptAnnotationIds": [draft.annotation_id],
            },
            stack.context,
        )
        assert replay.error is None, replay.error
        assert replay.payload["replayed"] is True
        assert replay.payload["acceptedPromptAnnotationIds"] == [draft.annotation_id]
        assert scheduled_titles == []

        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_pending_prompt_annotation_replay_restores_acceptance_ids(
    tmp_path: Path,
) -> None:
    """A lost staged-dispatch response must still clear accepted annotation drafts."""

    async with _open_real_stack(tmp_path / "pending-prompt-annotation.db") as stack:
        service, draft = await _create_html_prompt_annotation(
            stack,
            annotation_id="annotation-pending-replay-1",
        )
        staged = await get_dispatcher().dispatch(
            "pending-prompt-annotation-enqueue",
            "sessions.pending_inputs.enqueue",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-prompt-annotation-1",
                "clientRequestId": "pending-prompt-annotation-request",
                "clientMessageId": "pending-prompt-annotation-message",
                "message": "Apply the selected annotation.",
                "promptAnnotationIds": [draft.annotation_id],
            },
            stack.context,
        )
        assert staged.ok is True, staged.error
        assert staged.payload["promptAnnotationIds"] == [draft.annotation_id]

        dispatch_params = {
            "key": SESSION_KEY,
            "pendingInputId": "pending-prompt-annotation-1",
            "clientRequestId": "pending-prompt-annotation-request",
            "requestFingerprint": staged.payload["requestFingerprint"],
        }
        accepted = await get_dispatcher().dispatch(
            "pending-prompt-annotation-dispatch",
            "sessions.pending_inputs.dispatch",
            dispatch_params,
            stack.context,
        )
        assert accepted.ok is True, accepted.error
        assert accepted.payload["acceptedPromptAnnotationIds"] == [draft.annotation_id]
        sent = await service.get_prompt_annotation(draft.annotation_id)
        assert sent.status is PromptAnnotationStatus.SENT

        # The first response is deliberately treated as lost.  The staged row
        # is already consumed, so this exercises the receipt-only replay path
        # that no longer has promptAnnotationIds in its RPC params.
        replay = await get_dispatcher().dispatch(
            "pending-prompt-annotation-replay",
            "sessions.pending_inputs.dispatch",
            dispatch_params,
            stack.context,
        )
        assert replay.ok is True, replay.error
        assert replay.payload["replayed"] is True
        assert replay.payload["acceptedPromptAnnotationIds"] == [draft.annotation_id]
        assert replay.payload["task_id"] == accepted.payload["task_id"]
        assert replay.payload["message_id"] == accepted.payload["message_id"]

        stack.release_handler.set()
        await stack.runtime.wait(accepted.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_source", "expected_status", "expected_reason"),
    [
        (
            b"<html><body><section><h1 style='color:blue'>Original</h1></section></body></html>",
            "ready",
            None,
        ),
        (
            b"<html><body><h1>Original</h1><h1>Original</h1></body></html>",
            "contextual",
            "ambiguous",
        ),
    ],
    ids=["moved-and-restyled", "ambiguous"],
)
async def test_chat_send_normalizes_annotations_to_current_head_before_acceptance(
    tmp_path: Path,
    current_source: bytes,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    async with _open_real_stack(tmp_path / f"normalize-{expected_status}.db") as stack:
        service, draft = await _create_html_prompt_annotation(
            stack,
            annotation_id=f"annotation-normalize-{expected_status}",
        )
        document = await service.get_document(draft.document_id)
        ref = ArtifactStore(
            Path(stack.context.config.attachments.media_root or "")
        ).publish_bytes(
            current_source,
            session_id=stack.session_id,
            session_key=SESSION_KEY,
            name="page.html",
            mime="text/html",
            source="test-current-head",
        )
        current = await service.commit_revision(
            document_id=document.document_id,
            expected_head_revision_id=document.head_revision_id,
            expected_state_revision=document.state_revision,
            artifact=ArtifactBlobRef(
                artifact_id=ref.id,
                sha256=ref.sha256,
                filename=ref.name,
                media_type=ref.mime,
                byte_size=ref.size,
            ),
            actor=Actor(ActorKind.USER, "owner"),
        )

        response = await get_dispatcher().dispatch(
            f"rpc-normalize-{expected_status}",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "",
                "clientRequestId": f"normalize-{expected_status}",
                "promptAnnotationIds": [draft.annotation_id],
            },
            stack.context,
        )

        assert response.error is None, response.error
        await stack.wait_until_running()
        sent = await service.get_prompt_annotation(draft.annotation_id)
        assert sent.revision_id == current.revision.revision_id
        normalized_anchor = await service.get_anchor(sent.anchor_id)
        assert normalized_anchor.remapped_from_anchor_id == draft.anchor_id
        runtime_task = stack.runtime._tasks[response.payload["task_id"]]
        target = runtime_task.envelope.runtime_services["artifact_context"].targets[0]
        assert target.status == expected_status
        assert target.reason == expected_reason
        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_chat_send_reprepares_annotation_after_head_race_before_provider_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_real_stack(tmp_path / "annotation-head-race.db") as stack:
        service, draft = await _create_html_prompt_annotation(
            stack,
            annotation_id="annotation-head-race",
        )
        original_accept_turn = stack.storage.accept_turn
        acceptance_calls = 0
        raced_revision_id: str | None = None

        async def _accept_after_one_head_change(*args: Any, **kwargs: Any) -> Any:
            nonlocal acceptance_calls, raced_revision_id
            acceptance_calls += 1
            if acceptance_calls == 1:
                assert stack.handler_started.is_set() is False
                document = await service.get_document(draft.document_id)
                source = b"<html><body><section><h1>Original</h1></section></body></html>"
                ref = ArtifactStore(
                    Path(stack.context.config.attachments.media_root or "")
                ).publish_bytes(
                    source,
                    session_id=stack.session_id,
                    session_key=SESSION_KEY,
                    name="page.html",
                    mime="text/html",
                    source="test-head-race",
                )
                committed = await service.commit_revision(
                    document_id=document.document_id,
                    expected_head_revision_id=document.head_revision_id,
                    expected_state_revision=document.state_revision,
                    artifact=ArtifactBlobRef(
                        artifact_id=ref.id,
                        sha256=ref.sha256,
                        filename=ref.name,
                        media_type=ref.mime,
                        byte_size=ref.size,
                    ),
                    actor=Actor(ActorKind.USER, "owner"),
                )
                raced_revision_id = committed.revision.revision_id
            return await original_accept_turn(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "accept_turn", _accept_after_one_head_change)

        response = await get_dispatcher().dispatch(
            "rpc-annotation-head-race",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "",
                "clientRequestId": "annotation-head-race",
                "promptAnnotationIds": [draft.annotation_id],
            },
            stack.context,
        )

        assert response.error is None, response.error
        assert acceptance_calls == 2
        assert raced_revision_id is not None
        await stack.wait_until_running()
        sent = await service.get_prompt_annotation(draft.annotation_id)
        assert sent.revision_id == raced_revision_id
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }
        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_chat_send_first_ordinary_turn_schedules_auto_title_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        stack.context.config.naming.enabled = True
        await stack.manager.update(SESSION_KEY, display_name="WebChat")
        scheduled_titles: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def _record_auto_title(*args: Any, **kwargs: Any) -> None:
            scheduled_titles.append((args, kwargs))

        monkeypatch.setattr(
            "opensquilla.gateway.rpc_sessions._schedule_auto_title",
            _record_auto_title,
        )
        params = {
            "sessionKey": SESSION_KEY,
            "message": "Name this ordinary first turn.",
            "clientRequestId": "ordinary-first-turn-naming",
        }

        response = await get_dispatcher().dispatch(
            "rpc-ordinary-first-turn-naming",
            "chat.send",
            params,
            stack.context,
        )
        assert response.error is None, response.error
        await stack.wait_until_running()
        assert len(scheduled_titles) == 1
        assert scheduled_titles[0][0][2] == "Name this ordinary first turn."
        assert scheduled_titles[0][1]["enabled"] is True

        replay = await get_dispatcher().dispatch(
            "rpc-ordinary-first-turn-naming-replay",
            "chat.send",
            params,
            stack.context,
        )
        assert replay.error is None, replay.error
        assert replay.payload["replayed"] is True
        assert len(scheduled_titles) == 1

        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)


@pytest.mark.asyncio
async def test_pending_input_rpc_dispatch_is_exactly_once_across_response_replay(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "pending-input-dispatch.db") as stack:
        enqueue_params = {
            "key": SESSION_KEY,
            "pendingInputId": "pending-rpc-exactly-once",
            "clientRequestId": "pending-rpc-request",
            "clientMessageId": "pending-rpc-message",
            "message": "Run this queued follow-up exactly once.",
        }
        staged = await get_dispatcher().dispatch(
            "pending-enqueue",
            "sessions.pending_inputs.enqueue",
            enqueue_params,
            stack.context,
        )
        assert staged.ok is True
        assert staged.payload["status"] == "staged"
        staged_row = await stack.storage.get_pending_chat_input(
            "pending-rpc-exactly-once"
        )
        assert staged_row is not None

        listed = await get_dispatcher().dispatch(
            "pending-list",
            "sessions.pending_inputs.list",
            {"key": SESSION_KEY},
            stack.context,
        )
        assert listed.ok is True
        assert [item["pendingInputId"] for item in listed.payload["items"]] == [
            "pending-rpc-exactly-once"
        ]

        dispatch_params = {
            "key": SESSION_KEY,
            "pendingInputId": "pending-rpc-exactly-once",
            "clientRequestId": "pending-rpc-request",
            "requestFingerprint": staged.payload["requestFingerprint"],
        }
        missing_fingerprint = await get_dispatcher().dispatch(
            "pending-dispatch-missing-fingerprint",
            "sessions.pending_inputs.dispatch",
            {
                "key": SESSION_KEY,
                "pendingInputId": "pending-rpc-exactly-once",
                "clientRequestId": "pending-rpc-request",
            },
            stack.context,
        )
        assert missing_fingerprint.ok is False
        assert missing_fingerprint.error is not None
        assert missing_fingerprint.error.code == "PENDING_INPUT_FINGERPRINT_REQUIRED"
        accepted = await get_dispatcher().dispatch(
            "pending-dispatch",
            "sessions.pending_inputs.dispatch",
            dispatch_params,
            stack.context,
        )
        late_enqueue = await get_dispatcher().dispatch(
            "pending-enqueue-after-dispatch",
            "sessions.pending_inputs.enqueue",
            enqueue_params,
            stack.context,
        )
        assert late_enqueue.ok is False
        assert late_enqueue.error is not None
        assert late_enqueue.error.code == "PENDING_INPUT_ALREADY_DISPATCHED"
        changed_message_identity = await get_dispatcher().dispatch(
            "pending-changed-message-enqueue-after-dispatch",
            "sessions.pending_inputs.enqueue",
            {
                **enqueue_params,
                "clientMessageId": "pending-rpc-message-changed",
            },
            stack.context,
        )
        assert changed_message_identity.ok is False
        assert changed_message_identity.error is not None
        assert changed_message_identity.error.code == "PENDING_INPUT_CONFLICT"
        conflicting_late_enqueue = await get_dispatcher().dispatch(
            "pending-conflicting-enqueue-after-dispatch",
            "sessions.pending_inputs.enqueue",
            {
                **enqueue_params,
                "pendingInputId": "pending-rpc-late-arbitrary-id",
            },
            stack.context,
        )
        assert conflicting_late_enqueue.ok is False
        assert conflicting_late_enqueue.error is not None
        assert conflicting_late_enqueue.error.code == "PENDING_INPUT_CONFLICT"
        reused_message_identity = await get_dispatcher().dispatch(
            "pending-reused-message-enqueue-after-dispatch",
            "sessions.pending_inputs.enqueue",
            {
                **enqueue_params,
                "pendingInputId": "pending-rpc-late-new-request",
                "clientRequestId": "pending-rpc-request-new",
            },
            stack.context,
        )
        assert reused_message_identity.ok is False
        assert reused_message_identity.error is not None
        assert reused_message_identity.error.code == "PENDING_INPUT_CONFLICT"

        # Reproduce a queue ghost left by a pre-fix racing tab. Dispatch replay
        # must bind it to the original request/message receipt and consume it
        # without creating a second transcript or task.
        ghost_id = "pending-rpc-legacy-ghost"
        async with stack.storage._write_transaction(
            "test_insert_legacy_pending_ghost"
        ) as conn:
            await conn.execute(
                """
                INSERT INTO pending_chat_inputs (
                    pending_input_id, session_key, source_scope,
                    client_request_id, client_message_id, request_fingerprint,
                    payload_json, position, state_revision, created_at, updated_at,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, 1)
                """,
                (
                    ghost_id,
                    staged_row.session_key,
                    staged_row.source_scope,
                    staged_row.client_request_id,
                    staged_row.client_message_id,
                    staged_row.request_fingerprint,
                    json.dumps(
                        staged_row.payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    staged_row.updated_at + 1,
                    staged_row.updated_at + 1,
                ),
            )
        ghost_replayed = await get_dispatcher().dispatch(
            "pending-dispatch-legacy-ghost",
            "sessions.pending_inputs.dispatch",
            {**dispatch_params, "pendingInputId": ghost_id},
            stack.context,
        )
        assert ghost_replayed.ok is True
        assert ghost_replayed.payload["replayed"] is True
        assert await stack.storage.list_pending_chat_inputs(SESSION_KEY) == []

        replayed = await get_dispatcher().dispatch(
            "pending-dispatch-replay",
            "sessions.pending_inputs.dispatch",
            dispatch_params,
            stack.context,
        )

        assert accepted.ok is True
        assert replayed.ok is True
        assert replayed.payload["task_id"] == accepted.payload["task_id"]
        assert replayed.payload["message_id"] == accepted.payload["message_id"]
        arbitrary_pending = await get_dispatcher().dispatch(
            "pending-dispatch-arbitrary-id",
            "sessions.pending_inputs.dispatch",
            {
                **dispatch_params,
                "pendingInputId": "pending-rpc-arbitrary-id",
            },
            stack.context,
        )
        assert arbitrary_pending.ok is False
        assert arbitrary_pending.error is not None
        assert arbitrary_pending.error.code == "PENDING_INPUT_NOT_FOUND"
        assert await stack.storage.list_pending_chat_inputs(SESSION_KEY) == []
        counts = _table_counts(stack.db_path)
        assert counts == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_pending_input_cancel_tombstone_blocks_delayed_enqueue(
    tmp_path: Path,
) -> None:
    original_store = get_upload_store()
    upload_store = UploadStore(tmp_path / "cancel-first-upload-markers")
    set_upload_store(upload_store)
    try:
        async with _open_real_stack(
            tmp_path / "pending-input-cancel-first.db"
        ) as stack:
            identity = {
                "key": SESSION_KEY,
                "pendingInputId": "pending-rpc-cancel-first",
            }
            cancelled = await get_dispatcher().dispatch(
                "pending-cancel-before-enqueue",
                "sessions.pending_inputs.cancel",
                identity,
                stack.context,
            )
            assert cancelled.ok is True
            assert cancelled.payload["alreadyMissing"] is True

            payload = b"cancelled delayed attachment\n"
            digest = hashlib.sha256(payload).hexdigest()
            file_uuid = await upload_store.put("cancelled.txt", "text/plain", payload)
            delayed = await get_dispatcher().dispatch(
                "pending-delayed-enqueue",
                "sessions.pending_inputs.enqueue",
                {
                    **identity,
                    "clientRequestId": "pending-rpc-cancel-first-request",
                    "clientMessageId": "pending-rpc-cancel-first-message",
                    "message": "This delayed enqueue must stay cancelled.",
                    "attachments": [
                        {
                            "type": "text/plain",
                            "name": "cancelled.txt",
                            "file_uuid": file_uuid,
                        }
                    ],
                },
                stack.context,
            )
            assert delayed.ok is False
            assert delayed.error is not None
            assert delayed.error.code == "PENDING_INPUT_CANCELLED"
            assert await stack.storage.list_pending_chat_inputs(SESSION_KEY) == []
            assert not pending_chat_input_material_path(
                Path(stack.context.config.attachments.media_root or ""),
                stack.session_id,
                "pending-rpc-cancel-first",
                digest,
            ).exists()
    finally:
        set_upload_store(original_store)


@pytest.mark.asyncio
async def test_pending_input_rpc_rejects_conflicts_and_expired_attachments(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "pending-input-conflict.db") as stack:
        base = {
            "key": SESSION_KEY,
            "pendingInputId": "pending-rpc-conflict",
            "clientRequestId": "pending-rpc-conflict-request",
            "clientMessageId": "pending-rpc-conflict-message",
            "message": "original payload",
        }
        first = await get_dispatcher().dispatch(
            "pending-conflict-first",
            "sessions.pending_inputs.enqueue",
            base,
            stack.context,
        )
        assert first.ok is True

        conflict = await get_dispatcher().dispatch(
            "pending-conflict-second",
            "sessions.pending_inputs.enqueue",
            {**base, "message": "different payload"},
            stack.context,
        )
        assert conflict.ok is False
        assert conflict.error is not None
        assert conflict.error.code == "PENDING_INPUT_CONFLICT"

        attachment = await get_dispatcher().dispatch(
            "pending-attachment",
            "sessions.pending_inputs.enqueue",
            {
                **base,
                "pendingInputId": "pending-rpc-attachment",
                "clientRequestId": "pending-rpc-attachment-request",
                "clientMessageId": "pending-rpc-attachment-message",
                "attachments": [{"kind": "staged", "file_uuid": "expiring-token"}],
            },
            stack.context,
        )
        assert attachment.ok is False
        assert attachment.error is not None
        assert attachment.error.code == "ATTACHMENT_EXPIRED"
        assert len(await stack.storage.list_pending_chat_inputs(SESSION_KEY)) == 1


@pytest.mark.asyncio
async def test_pending_attachment_survives_restart_dispatches_once_and_cleans_owner(
    tmp_path: Path,
) -> None:
    original_store = get_upload_store()
    upload_store = UploadStore(tmp_path / "upload-markers")
    set_upload_store(upload_store)
    try:
        async with _open_real_stack(tmp_path / "pending-attachment.db") as stack:
            payload = b"durable queued attachment\n"
            digest = hashlib.sha256(payload).hexdigest()
            file_uuid = await upload_store.put("queued.txt", "text/plain", payload)
            enqueue_params = {
                "key": SESSION_KEY,
                "pendingInputId": "pending-rpc-durable-attachment",
                "clientRequestId": "pending-rpc-durable-attachment-request",
                "clientMessageId": "pending-rpc-durable-attachment-message",
                "message": "Use the durable attachment.",
                "attachments": [
                    {
                        "type": "text/plain",
                        "mime": "text/plain",
                        "name": "queued.txt",
                        "file_uuid": file_uuid,
                    }
                ],
            }
            staged = await get_dispatcher().dispatch(
                "pending-attachment-enqueue",
                "sessions.pending_inputs.enqueue",
                enqueue_params,
                stack.context,
            )
            assert staged.ok is True
            assert staged.payload["attachments"] == [
                {
                    "name": "queued.txt",
                    "mime": "text/plain",
                    "type": "text/plain",
                    "size": len(payload),
                }
            ]

            row = await stack.storage.get_pending_chat_input(
                "pending-rpc-durable-attachment"
            )
            assert row is not None
            assert row.payload["attachments"] == [
                {
                    "kind": "attachment_ref",
                    "type": "text/plain",
                    "mime": "text/plain",
                    "name": "queued.txt",
                    "size": len(payload),
                    "sha256": digest,
                    "material_id": digest,
                    "store": PENDING_CHAT_INPUT_MATERIAL_STORE,
                    "scope": stack.session_id,
                    "pending_input_id": "pending-rpc-durable-attachment",
                    "source": "upload",
                    "_was_staged": True,
                }
            ]
            owner_path = pending_chat_input_material_path(
                Path(stack.context.config.attachments.media_root or ""),
                stack.session_id,
                "pending-rpc-durable-attachment",
                digest,
            )
            assert owner_path.read_bytes() == payload

            # The expiring upload is gone and the process-local upload store is
            # replaced, matching a Gateway restart. Dispatch must use only the
            # durable material reference retained by SQLite.
            assert file_uuid not in upload_store._entries
            set_upload_store(UploadStore(tmp_path / "upload-markers"))
            enqueue_replay = await get_dispatcher().dispatch(
                "pending-attachment-enqueue-replay",
                "sessions.pending_inputs.enqueue",
                enqueue_params,
                stack.context,
            )
            assert enqueue_replay.ok is True
            assert enqueue_replay.payload["replayed"] is True
            assert (
                enqueue_replay.payload["requestFingerprint"]
                == staged.payload["requestFingerprint"]
            )
            dispatch_params = {
                "key": SESSION_KEY,
                "pendingInputId": "pending-rpc-durable-attachment",
                "clientRequestId": "pending-rpc-durable-attachment-request",
                "requestFingerprint": staged.payload["requestFingerprint"],
            }
            accepted = await get_dispatcher().dispatch(
                "pending-attachment-dispatch",
                "sessions.pending_inputs.dispatch",
                dispatch_params,
                stack.context,
            )
            replayed = await get_dispatcher().dispatch(
                "pending-attachment-dispatch-replay",
                "sessions.pending_inputs.dispatch",
                dispatch_params,
                stack.context,
            )
            assert accepted.ok is True
            assert replayed.ok is True
            assert replayed.payload["message_id"] == accepted.payload["message_id"]
            assert not owner_path.exists()
            assert transcript_material_path(
                Path(stack.context.config.attachments.media_root or ""),
                stack.session_id,
                digest,
            ).read_bytes() == payload
            assert _table_counts(stack.db_path) == {
                "transcript_entries": 1,
                "agent_tasks": 1,
                "turn_ingress_receipts": 1,
            }
    finally:
        set_upload_store(original_store)


@pytest.mark.asyncio
async def test_pending_attachment_cancel_removes_only_its_private_owner(
    tmp_path: Path,
) -> None:
    original_store = get_upload_store()
    upload_store = UploadStore(tmp_path / "cancel-upload-markers")
    set_upload_store(upload_store)
    try:
        async with _open_real_stack(tmp_path / "pending-attachment-cancel.db") as stack:
            payload = b"cancel this queued attachment\n"
            digest = hashlib.sha256(payload).hexdigest()
            file_uuid = await upload_store.put("cancel.txt", "text/plain", payload)
            staged = await get_dispatcher().dispatch(
                "pending-attachment-cancel-enqueue",
                "sessions.pending_inputs.enqueue",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-cancel-attachment",
                    "clientRequestId": "pending-rpc-cancel-attachment-request",
                    "clientMessageId": "pending-rpc-cancel-attachment-message",
                    "message": "Queue then cancel this attachment.",
                    "attachments": [
                        {
                            "type": "text/plain",
                            "name": "cancel.txt",
                            "file_uuid": file_uuid,
                        }
                    ],
                },
                stack.context,
            )
            assert staged.ok is True
            media_root = Path(stack.context.config.attachments.media_root or "")
            owner_path = pending_chat_input_material_path(
                media_root,
                stack.session_id,
                "pending-rpc-cancel-attachment",
                digest,
            )
            canonical_path = transcript_material_path(
                media_root,
                stack.session_id,
                digest,
            )
            assert owner_path.read_bytes() == payload
            assert not canonical_path.exists()

            cancelled = await get_dispatcher().dispatch(
                "pending-attachment-cancel",
                "sessions.pending_inputs.cancel",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-cancel-attachment",
                    "expectedRevision": staged.payload["revision"],
                },
                stack.context,
            )
            assert cancelled.ok is True
            assert not owner_path.exists()
            assert not canonical_path.exists()
            assert await stack.storage.list_pending_chat_inputs(SESSION_KEY) == []
    finally:
        set_upload_store(original_store)


@pytest.mark.asyncio
async def test_session_delete_reclaims_pending_attachment_owner(
    tmp_path: Path,
) -> None:
    original_store = get_upload_store()
    upload_store = UploadStore(tmp_path / "delete-upload-markers")
    set_upload_store(upload_store)
    try:
        async with _open_real_stack(tmp_path / "pending-attachment-delete.db") as stack:
            payload = b"delete this session-owned pending attachment\n"
            digest = hashlib.sha256(payload).hexdigest()
            file_uuid = await upload_store.put("delete.txt", "text/plain", payload)
            staged = await get_dispatcher().dispatch(
                "pending-attachment-delete-enqueue",
                "sessions.pending_inputs.enqueue",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-delete-attachment",
                    "clientRequestId": "pending-rpc-delete-attachment-request",
                    "clientMessageId": "pending-rpc-delete-attachment-message",
                    "message": "Delete this queued attachment with the session.",
                    "attachments": [
                        {
                            "type": "text/plain",
                            "name": "delete.txt",
                            "file_uuid": file_uuid,
                        }
                    ],
                },
                stack.context,
            )
            assert staged.ok is True
            owner_path = pending_chat_input_material_path(
                Path(stack.context.config.attachments.media_root or ""),
                stack.session_id,
                "pending-rpc-delete-attachment",
                digest,
            )
            assert owner_path.read_bytes() == payload

            deleted = await get_dispatcher().dispatch(
                "pending-attachment-session-delete",
                "sessions.delete",
                {"key": SESSION_KEY},
                stack.context,
            )
            assert deleted.ok is True
            assert deleted.payload == {"deleted": [SESSION_KEY], "errors": []}
            assert not owner_path.exists()
            assert await stack.storage.get_pending_chat_input(
                "pending-rpc-delete-attachment"
            ) is None
    finally:
        set_upload_store(original_store)


@pytest.mark.asyncio
async def test_cancel_cleans_unreferenced_canonical_copy_after_failed_dispatch(
    tmp_path: Path,
) -> None:
    original_store = get_upload_store()
    upload_store = UploadStore(tmp_path / "failed-dispatch-upload-markers")
    set_upload_store(upload_store)
    try:
        async with _open_real_stack(
            tmp_path / "pending-attachment-failed-dispatch.db",
            max_pending_per_session=1,
        ) as stack:
            payload = b"failed dispatch must not leak canonical bytes\n"
            digest = hashlib.sha256(payload).hexdigest()
            file_uuid = await upload_store.put("failed.txt", "text/plain", payload)
            staged = await get_dispatcher().dispatch(
                "pending-attachment-failed-enqueue",
                "sessions.pending_inputs.enqueue",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-failed-attachment",
                    "clientRequestId": "pending-rpc-failed-attachment-request",
                    "clientMessageId": "pending-rpc-failed-attachment-message",
                    "message": "This dispatch will hit queue capacity.",
                    "attachments": [
                        {
                            "type": "text/plain",
                            "name": "failed.txt",
                            "file_uuid": file_uuid,
                        }
                    ],
                },
                stack.context,
            )
            assert staged.ok is True
            blocker = await stack.runtime.reserve(
                RouteEnvelope(
                    source_kind=SourceKind.WEB,
                    source_name="pending-attachment-capacity-test",
                    agent_id="main",
                    session_key=SESSION_KEY,
                    input_provenance={"kind": "synthetic-test"},
                ),
                "reserve the only queue slot",
            )
            dispatch = await get_dispatcher().dispatch(
                "pending-attachment-failed-dispatch",
                "sessions.pending_inputs.dispatch",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-failed-attachment",
                    "clientRequestId": "pending-rpc-failed-attachment-request",
                    "requestFingerprint": staged.payload["requestFingerprint"],
                },
                stack.context,
            )
            assert dispatch.ok is False
            assert dispatch.error is not None
            assert dispatch.error.code == "QUEUE_FULL"
            canonical_path = transcript_material_path(
                Path(stack.context.config.attachments.media_root or ""),
                stack.session_id,
                digest,
            )
            assert canonical_path.read_bytes() == payload

            cancelled = await get_dispatcher().dispatch(
                "pending-attachment-failed-cancel",
                "sessions.pending_inputs.cancel",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-failed-attachment",
                    "expectedRevision": staged.payload["revision"],
                },
                stack.context,
            )
            assert cancelled.ok is True
            assert not canonical_path.exists()
            await stack.runtime.abort_reservation(blocker)
    finally:
        set_upload_store(original_store)


@pytest.mark.asyncio
async def test_cancel_preserves_canonical_material_referenced_by_transcript(
    tmp_path: Path,
) -> None:
    original_store = get_upload_store()
    upload_store = UploadStore(tmp_path / "shared-material-upload-markers")
    set_upload_store(upload_store)
    try:
        async with _open_real_stack(
            tmp_path / "pending-attachment-shared-material.db",
            max_pending_per_session=1,
        ) as stack:
            payload = b"shared transcript attachment bytes\n"
            digest = hashlib.sha256(payload).hexdigest()
            media_root = Path(stack.context.config.attachments.media_root or "")
            canonical_path = transcript_material_path(
                media_root,
                stack.session_id,
                digest,
            )
            canonical_path.parent.mkdir(parents=True, exist_ok=True)
            canonical_path.write_bytes(payload)
            await stack.manager.append_message(
                SESSION_KEY,
                role="user",
                content=json.dumps(
                    {
                        "text": "existing transcript reference",
                        "attachments": [
                            {
                                "sha256_ref": digest,
                                "name": "existing.txt",
                                "mime": "text/plain",
                                "size": len(payload),
                            }
                        ],
                    }
                ),
            )
            file_uuid = await upload_store.put("shared.txt", "text/plain", payload)
            staged = await get_dispatcher().dispatch(
                "pending-shared-material-enqueue",
                "sessions.pending_inputs.enqueue",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-shared-material",
                    "clientRequestId": "pending-rpc-shared-material-request",
                    "clientMessageId": "pending-rpc-shared-material-message",
                    "message": "Do not delete the shared canonical material.",
                    "attachments": [
                        {
                            "type": "text/plain",
                            "name": "shared.txt",
                            "file_uuid": file_uuid,
                        }
                    ],
                },
                stack.context,
            )
            assert staged.ok is True
            blocker = await stack.runtime.reserve(
                RouteEnvelope(
                    source_kind=SourceKind.WEB,
                    source_name="pending-shared-material-capacity-test",
                    agent_id="main",
                    session_key=SESSION_KEY,
                    input_provenance={"kind": "synthetic-test"},
                ),
                "reserve the only queue slot",
            )
            dispatch = await get_dispatcher().dispatch(
                "pending-shared-material-dispatch",
                "sessions.pending_inputs.dispatch",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-shared-material",
                    "clientRequestId": "pending-rpc-shared-material-request",
                    "requestFingerprint": staged.payload["requestFingerprint"],
                },
                stack.context,
            )
            assert dispatch.ok is False
            assert dispatch.error is not None
            assert dispatch.error.code == "QUEUE_FULL"
            cancelled = await get_dispatcher().dispatch(
                "pending-shared-material-cancel",
                "sessions.pending_inputs.cancel",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": "pending-rpc-shared-material",
                    "expectedRevision": staged.payload["revision"],
                },
                stack.context,
            )
            assert cancelled.ok is True
            assert canonical_path.read_bytes() == payload
            await stack.runtime.abort_reservation(blocker)
    finally:
        set_upload_store(original_store)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control_kind", "message", "request_id", "correlation_id", "stage_options"),
    [
        (
            "manual",
            "/meta meta-tiny -- run outside the active goal",
            "active-goal-manual-meta-control",
            "request:active-goal-manual-meta-control",
            {},
        ),
        (
            "replay",
            format_meta_replay_sentinel("0123456789abcdef0123456789abcdef"),
            "active-goal-replay-meta-control",
            "nonce:0123456789abcdef0123456789abcdef",
            {"replay_run_id": "source-run-active-goal", "replay_mode": "failed-step"},
        ),
    ],
    ids=["manual", "replay"],
)
async def test_durable_meta_control_does_not_claim_active_goal(
    tmp_path: Path,
    control_kind: str,
    message: str,
    request_id: str,
    correlation_id: str,
    stage_options: dict[str, str],
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        seeded_goal = await _seed_idle_active_goal(stack)
        intent, disposition = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind=control_kind,
            correlation_id=correlation_id,
            meta_skill_name="meta-tiny",
            **stage_options,
        )
        assert disposition == "stamped"

        response = await get_dispatcher().dispatch(
            f"rpc-{control_kind}-meta-control-with-active-goal",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": message,
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        accepted_control = await stack.storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind=control_kind,
            correlation_id=correlation_id,
        )
        assert accepted_control is not None
        assert accepted_control.intent_id == intent.intent_id
        assert accepted_control.status == "accepted"
        assert accepted_control.accepted_task_id == response.payload["task_id"]
        control_task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert control_task is not None
        assert control_task.details is not None
        assert control_task.details["goal_context"] is None
        assert control_task.details["goal_candidate"] is None

        current_goal = await stack.storage.get_goal(SESSION_KEY)
        assert current_goal is not None
        assert current_goal.goal_id == seeded_goal.goal_id
        assert current_goal.status == "active"
        assert current_goal.active_task_id is None
        assert current_goal.state_revision == seeded_goal.state_revision


@pytest.mark.asyncio
async def test_legacy_meta_launch_does_not_claim_active_goal(tmp_path: Path) -> None:
    request_id = "legacy-meta-launch-with-active-goal"
    assert (
        pending_meta_launch_put(
            SESSION_KEY,
            "meta-tiny",
            client_request_id=request_id,
        )
        == "stamped"
    )

    try:
        async with _open_real_stack(tmp_path / "legacy-meta-active-goal.db") as stack:
            seeded_goal = await _seed_idle_active_goal(stack)
            response = await get_dispatcher().dispatch(
                "rpc-legacy-meta-launch-with-active-goal",
                "chat.send",
                {
                    "sessionKey": SESSION_KEY,
                    "message": "/meta meta-tiny",
                    "clientRequestId": request_id,
                },
                stack.context,
            )
            await stack.wait_until_running()

            assert response.ok is True
            task = await stack.storage.get_agent_task(response.payload["task_id"])
            assert task is not None and task.details is not None
            assert task.details.get("goal_context") is None
            assert task.details.get("goal_candidate") is None

            current_goal = await stack.storage.get_goal(SESSION_KEY)
            assert current_goal is not None
            assert current_goal.goal_id == seeded_goal.goal_id
            assert current_goal.status == "active"
            assert current_goal.active_task_id is None
            assert current_goal.state_revision == seeded_goal.state_revision
    finally:
        pending_meta_launch_pop(SESSION_KEY, client_request_id=request_id)


@pytest.mark.asyncio
async def test_default_turn_claims_goal_inside_atomic_acceptance_without_pre_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_real_stack(tmp_path / "atomic-current-goal-claim.db") as stack:
        seeded_goal = await _seed_idle_active_goal(stack)
        original_get_goal = stack.storage.get_goal

        async def reject_pre_accept_read(_session_key: str) -> None:
            pytest.fail("sessions.send must resolve the current Goal inside accept_turn")

        monkeypatch.setattr(stack.storage, "get_goal", reject_pre_accept_read)
        response = await get_dispatcher().dispatch(
            "rpc-default-turn-current-goal-claim",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "Continue the active Goal atomically.",
                "clientRequestId": "atomic-current-goal-claim",
            },
            stack.context,
        )

        assert response.ok is True
        accepted_task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert accepted_task is not None and accepted_task.details is not None
        goal_context = accepted_task.details.get("goal_context")
        assert isinstance(goal_context, dict)
        assert goal_context["goalId"] == seeded_goal.goal_id
        assert goal_context["taskId"] == response.payload["task_id"]
        assert accepted_task.details.get("goal_candidate") is None

        # The minimal ingress stack intentionally has no GoalService lifecycle
        # listener.  Activation may therefore fail closed and settle the owner
        # immediately, but the durable task must retain the context resolved in
        # the acceptance transaction.
        current_goal = await original_get_goal(SESSION_KEY)
        assert current_goal is not None and current_goal.goal_id == seeded_goal.goal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attachment",
    [
        {"type": "image/png", "name": "synthetic.png", "data": "aW1hZ2U="},
        {"type": "text/plain", "name": "synthetic.txt", "data": "dGV4dA=="},
    ],
    ids=["image", "file"],
)
async def test_prompt_annotations_reject_attachments_before_runtime_acceptance(
    tmp_path: Path,
    attachment: dict[str, str],
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        service, draft = await _create_html_prompt_annotation(
            stack,
            annotation_id="annotation-with-attachment",
        )

        response = await get_dispatcher().dispatch(
            "rpc-prompt-annotation-attachment-rejected",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "Apply this annotation and inspect the image.",
                "clientRequestId": "prompt-annotation-attachment-rejected",
                "promptAnnotationIds": [draft.annotation_id],
                "attachments": [attachment],
            },
            stack.context,
        )

        assert response.error is not None
        assert response.error.code == "PROMPT_ANNOTATION_ATTACHMENTS_UNSUPPORTED"
        assert response.error.accepted is False
        assert (await service.get_prompt_annotation(draft.annotation_id)).status is (
            PromptAnnotationStatus.DRAFT
        )
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        _assert_no_runtime_acceptance_state(stack.runtime)
        assert stack.handler_started.is_set() is False


@pytest.mark.asyncio
async def test_prompt_annotations_reject_plan_mode_before_runtime_acceptance(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        service, draft = await _create_html_prompt_annotation(
            stack,
            annotation_id="annotation-in-plan-mode",
        )
        await stack.storage.set_collaboration_mode(
            SESSION_KEY,
            "plan",
            expected_revision=0,
        )

        response = await get_dispatcher().dispatch(
            "rpc-prompt-annotation-plan-rejected",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "Apply this annotation while planning.",
                "clientRequestId": "prompt-annotation-plan-rejected",
                "promptAnnotationIds": [draft.annotation_id],
            },
            stack.context,
        )

        assert response.error is not None
        assert response.error.code == "ARTIFACT_PROMPT_ANNOTATIONS_PLAN_UNSUPPORTED"
        assert response.error.accepted is False
        assert (await service.get_prompt_annotation(draft.annotation_id)).status is (
            PromptAnnotationStatus.DRAFT
        )
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        _assert_no_runtime_acceptance_state(stack.runtime)
        assert stack.handler_started.is_set() is False


@pytest.mark.asyncio
async def test_prompt_annotation_attachment_rule_runs_after_receipt_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        params = {
            "sessionKey": SESSION_KEY,
            "message": "Legacy accepted annotation with an image.",
            "clientRequestId": "legacy-annotation-attachment",
            "promptAnnotationIds": ["legacy-annotation"],
            "attachments": [{"type": "image/png", "name": "legacy.png"}],
        }
        receipt = TurnIngressReceipt(
            source_scope="web:web:operator",
            request_session_key=SESSION_KEY,
            client_request_id="legacy-annotation-attachment",
            request_fingerprint=request_fingerprint(params),
            accepted_session_key=SESSION_KEY,
            session_id=stack.session_id,
            message_id="legacy-message",
            task_id=None,
            accepted_at=1,
        )

        async def _replay(**_kwargs: object) -> TurnAcceptanceResult:
            return TurnAcceptanceResult(
                receipt=receipt,
                replayed=True,
                fresh_user_session=False,
            )

        monkeypatch.setattr(stack.storage, "replay_turn_ingress_receipt", _replay)

        response = await get_dispatcher().dispatch(
            "rpc-legacy-prompt-annotation-attachment-replay",
            "chat.send",
            params,
            stack.context,
        )

        assert response.error is None, response.error
        assert response.payload["replayed"] is True
        assert response.payload["acceptedPromptAnnotationIds"] == ["legacy-annotation"]
        _assert_no_runtime_acceptance_state(stack.runtime)
        assert stack.handler_started.is_set() is False


@pytest.mark.asyncio
async def test_durable_manual_meta_control_survives_memory_loss_and_long_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "durable-meta-control-after-restart"
    db_path = tmp_path / "sessions.db"
    response_payload: dict[str, Any]
    async with _open_real_stack(db_path) as stack:
        intent, disposition = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-tiny",
        )
        assert disposition == "stamped"
        # No in-process launch cache exists: this models a Gateway restart and
        # also proves the old 15-minute monotonic staging TTL is irrelevant.
        assert pending_meta_launch_peek(SESSION_KEY, client_request_id=request_id) is None
        monkeypatch.setattr(
            "opensquilla.engine.steps.meta_command.time.monotonic",
            lambda: 10**12,
        )

        response = await get_dispatcher().dispatch(
            "rpc-durable-meta-control",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "/meta meta-tiny -- write a durable paper",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()
        assert response.ok is True
        response_payload = dict(response.payload)

        accepted = await stack.storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        )
        assert accepted is not None
        assert accepted.intent_id == intent.intent_id
        assert accepted.status == "accepted"
        assert accepted.accepted_task_id == response.payload["task_id"]
        accepted_task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert accepted_task is not None
        assert accepted_task.queue_mode == "followup"

        duplicate = await get_dispatcher().dispatch(
            "rpc-durable-meta-control-duplicate",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "/meta meta-tiny -- write a durable paper",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        assert duplicate.ok is True
        assert duplicate.payload["replayed"] is True
        assert duplicate.payload["task_id"] == response.payload["task_id"]
        stack.release_handler.set()
        await stack.runtime.wait(response.payload["task_id"], timeout=2.0)

    # Reopen SQLite after the accepted task completed. The exact server-bound
    # control remains on the transcript and can seed the engine without any
    # module-level pending marker.
    reopened = await SessionStorage.open(str(db_path))
    try:
        entries = await reopened.get_transcript(response_payload["session_id"])
        turn_context = entries[0].turn_context
        assert isinstance(turn_context, dict)
        assert turn_context["meta_control"]["intent_id"] == intent.intent_id
        launch_turn = SimpleNamespace(
            session_key=SESSION_KEY,
            message="/meta meta-tiny -- write a durable paper",
            semantic_message="/meta meta-tiny -- write a durable paper",
            metadata={},
        )
        with turn_context_scope(turn_context):
            await meta_command_launch(launch_turn)
        assert launch_turn.metadata["meta_launch"] == {
            "name": "meta-tiny",
            "request": "write a durable paper",
        }
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_durable_meta_control_ordinary_turn_cannot_consume(
    tmp_path: Path,
) -> None:
    request_id = "durable-meta-control-mismatch"
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        intent, _ = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-tiny",
        )
        response = await get_dispatcher().dispatch(
            "rpc-durable-meta-control-mismatch",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "an ordinary message must not claim the staged control",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()
        assert response.ok is True
        untouched = await stack.storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
        )
        assert untouched is not None
        assert untouched.intent_id == intent.intent_id
        assert untouched.status == "staged"
        entries = await stack.storage.get_transcript(stack.session_id)
        assert "meta_control" not in (entries[0].turn_context or {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "/meta unstaged-skill",
        "/meta-replay 0123456789abcdef0123456789abcdef",
    ],
)
async def test_request_bound_meta_control_without_matching_stage_fails_closed(
    tmp_path: Path,
    message: str,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-unstaged-meta-control",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": message,
                "clientRequestId": "unstaged-meta-control",
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "META_CONTROL_NOT_STAGED"
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }


@pytest.mark.asyncio
async def test_same_key_reset_invalidates_control_retained_by_another_client(
    tmp_path: Path,
) -> None:
    request_id = "meta-control-staged-before-reset"
    launch_text = "/meta meta-tiny -- must not cross the reset boundary"
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        original_session_id = stack.session_id
        staged, _ = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=f"request:{request_id}",
            meta_skill_name="meta-tiny",
        )

        reset = await get_dispatcher().dispatch(
            "rpc-reset-with-staged-control",
            "sessions.reset",
            {"key": SESSION_KEY},
            stack.context,
        )
        assert reset.ok is True
        assert reset.payload["session_id"] != original_session_id
        assert reset.payload["epoch"] == 1
        assert await stack.storage.get_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="manual",
            correlation_id=staged.correlation_id,
        ) is None

        # Model a second tab whose browser outbox still holds the pre-reset
        # marker. Server-side reset fencing must reject it independently of any
        # client cleanup or synchronization.
        stale_send = await get_dispatcher().dispatch(
            "rpc-stale-control-after-reset",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": launch_text,
                "clientRequestId": request_id,
            },
            stack.context,
        )
        assert stale_send.ok is False
        assert stale_send.error is not None
        assert stale_send.error.code == "META_CONTROL_NOT_STAGED"
        assert stale_send.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }


@pytest.mark.asyncio
async def test_durable_failed_step_replay_survives_commit_then_memory_loss(
    tmp_path: Path,
) -> None:
    nonce = "0123456789abcdef0123456789abcdef"
    request_id = "durable-replay-control"
    db_path = tmp_path / "sessions.db"
    async with _open_real_stack(db_path) as stack:
        intent, _ = await stack.storage.stage_meta_control_intent(
            session_key=SESSION_KEY,
            control_kind="replay",
            correlation_id=f"nonce:{nonce}",
            meta_skill_name="meta-tiny",
            replay_run_id="source-run-1",
            replay_mode="failed-step",
        )
        assert pending_meta_launch_peek(SESSION_KEY, client_request_id=request_id) is None
        launch_text = format_meta_replay_sentinel(nonce)
        response = await get_dispatcher().dispatch(
            "rpc-durable-replay-control",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": launch_text,
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()
        assert response.ok is True
        entries = await stack.storage.get_transcript(stack.session_id)
        turn_context = entries[0].turn_context
        assert isinstance(turn_context, dict)
        assert turn_context["meta_control"]["intent_id"] == intent.intent_id

        replay_turn = SimpleNamespace(
            session_key=SESSION_KEY,
            message=launch_text,
            semantic_message=launch_text,
            metadata={},
        )
        with turn_context_scope(turn_context):
            await meta_command_launch(replay_turn)
        assert replay_turn.metadata["meta_replay"] == {
            "run_id": "source-run-1",
            "name": "meta-tiny",
            "mode": "failed-step",
        }


@pytest.mark.asyncio
async def test_queued_meta_control_reopens_and_reactivates_exactly_once(
    tmp_path: Path,
) -> None:
    """A crash after acceptance but before RUNNING cannot lose the launch."""

    db_path = tmp_path / "sessions.db"
    storage = await SessionStorage.open(str(db_path))
    manager = SessionManager(storage)
    session = await manager.create(SESSION_KEY, agent_id="main")
    blocker_started = asyncio.Event()
    hold_blocker = asyncio.Event()
    gateway_config = GatewayConfig(
        workspace_dir=str(tmp_path / "workspace"),
        memory={"flush_enabled": False},
        naming={"enabled": False},
    )
    routing_state: dict[str, Any] = {"mode": "router", "revision": 7}

    def _accepted_routing_provider(*, session_key: str, run_kind: str) -> Any:
        assert session_key == SESSION_KEY
        assert run_kind in {"session_turn", "recovery_model_routing_base"}
        return capture_model_routing_config(
            gateway_config,
            session_mode=routing_state["mode"],
            session_routing_revision=routing_state["revision"],
            session_routing_source="session",
        )

    async def _blocking_handler(_run: Any) -> None:
        blocker_started.set()
        await hold_blocker.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_blocking_handler,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
        accepted_config_provider=_accepted_routing_provider,
    )
    context = RpcContext(
        conn_id="meta-restart-before-start",
        principal=_PRINCIPAL,
        config=gateway_config,
        session_manager=manager,
        task_runtime=runtime,
    )
    blocker = await get_dispatcher().dispatch(
        "rpc-meta-restart-blocker",
        "chat.send",
        {
            "sessionKey": SESSION_KEY,
            "message": "occupy the only runtime slot",
            "clientRequestId": "meta-restart-blocker",
        },
        context,
    )
    assert blocker.ok is True
    await asyncio.wait_for(blocker_started.wait(), timeout=2.0)

    request_id = "meta-restart-accepted-control"
    launch_text = "/meta meta-tiny -- preserve exact semantic input"
    await storage.stage_meta_control_intent(
        session_key=SESSION_KEY,
        control_kind="manual",
        correlation_id=f"request:{request_id}",
        meta_skill_name="meta-tiny",
    )
    accepted = await get_dispatcher().dispatch(
        "rpc-meta-restart-control",
        "chat.send",
        {
            "sessionKey": SESSION_KEY,
            "message": launch_text,
            "clientRequestId": request_id,
        },
        context,
    )
    assert accepted.ok is True
    task_id = accepted.payload["task_id"]
    queued = await storage.get_agent_task(task_id)
    assert queued is not None
    assert queued.status == "queued"
    assert queued.details is not None
    assert queued.details["meta_control_message"] == launch_text
    assert queued.details["meta_control_semantic_message"] == launch_text
    assert queued.details["accepted_model_routing"]["session_mode"] == "router"
    assert queued.details["accepted_model_routing"]["session_revision"] == 7
    transcript = await storage.get_transcript(session.session_id)
    control_entry = next(
        entry
        for entry in transcript
        if entry.message_id == accepted.payload["message_id"]
    )
    assert control_entry.content != launch_text  # SessionManager applied its timestamp prefix.

    # Model an abrupt process loss: close SQLite before cancelling in-memory
    # coroutines, so their cancellation cleanup cannot rewrite durable state.
    old_async_tasks = [
        task.asyncio_task
        for task in runtime._tasks.values()
        if task.asyncio_task is not None
    ]
    await storage.close()
    for old_task in old_async_tasks:
        old_task.cancel()
    await asyncio.gather(*old_async_tasks, return_exceptions=True)

    reopened = await SessionStorage.open(str(db_path))
    routing_state.update(mode="direct", revision=8)
    recovered_runs: list[tuple[Any, dict[str, Any] | None]] = []

    async def _capture_recovered(run: Any) -> None:
        turn_context = current_turn_context()
        recovered_runs.append((run, dict(turn_context) if turn_context is not None else None))

    recovered_runtime = TaskRuntime(
        storage=reopened,
        turn_handler=_capture_recovered,
        max_concurrency=1,
        running_heartbeat_interval_s=None,
        accepted_config_provider=_accepted_routing_provider,
    )
    try:
        abandoned = await reopened.get_agent_task(task_id)
        assert abandoned is not None
        assert abandoned.status == "abandoned"
        assert abandoned.terminal_reason == "meta_control_restart_before_start"

        assert await recovered_runtime.recover_durable_meta_controls() == 1
        completed = await recovered_runtime.wait(task_id, timeout=2.0)
        assert completed.status == "succeeded"
        assert len(recovered_runs) == 1
        recovered_run, recovered_context = recovered_runs[0]
        assert recovered_run.task_id == task_id
        assert recovered_run.message == launch_text
        assert recovered_run.semantic_message == launch_text
        assert recovered_run.accepted_config.session_mode == "router"
        assert recovered_run.accepted_config.session_routing_revision == 7
        assert recovered_run.accepted_config.session_routing_source == "session"
        assert recovered_context is not None
        assert recovered_context["meta_control"]["name"] == "meta-tiny"

        # A second recovery pass and a response-loss retry both reuse the
        # accepted identity without creating or executing another task.
        assert await recovered_runtime.recover_durable_meta_controls() == 0
        reopened_manager = SessionManager(reopened)
        reopened_context = RpcContext(
            conn_id="meta-restart-retry",
            principal=_PRINCIPAL,
            config=context.config,
            session_manager=reopened_manager,
            task_runtime=recovered_runtime,
        )
        duplicate = await get_dispatcher().dispatch(
            "rpc-meta-restart-control-duplicate",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": launch_text,
                "clientRequestId": request_id,
            },
            reopened_context,
        )
        assert duplicate.ok is True
        assert duplicate.payload["replayed"] is True
        assert duplicate.payload["task_id"] == task_id
        assert len(recovered_runs) == 1
        assert _table_counts(db_path) == {
            "transcript_entries": 2,
            "agent_tasks": 2,
            "turn_ingress_receipts": 2,
        }
    finally:
        await recovered_runtime.shutdown(cancel=True, timeout=2.0)
        await reopened.close()


@pytest.mark.asyncio
async def test_meta_control_recovery_is_nonblocking_and_fair_to_other_sessions() -> None:
    session_key = "agent:main:webchat:recovery-batches"
    records: dict[str, AgentTaskRecord] = {}
    claims: list[Any] = []
    for index in range(3):
        task_id = f"recovery-task-{index}"
        message_id = f"recovery-message-{index}"
        message = f"/meta meta-tiny -- batch {index}"
        control = {
            "version": 1,
            "intent_id": f"intent-{index}",
            "kind": "manual",
            "name": "meta-tiny",
            "correlation_id": f"request:batch-{index}",
        }
        task = AgentTaskRecord(
            task_id=task_id,
            session_key=session_key,
            agent_id="main",
            source_kind="web",
            queue_mode="interrupt",
            run_kind="session_turn",
            status=AgentTaskStatus.ABANDONED,
            terminal_reason="meta_control_restart_before_start",
            details={
                "source_name": "RPC",
                "input_provenance": {},
                "metadata": {"meta_control": control},
                "persisted_user_message_id": message_id,
                "persisted_user_message_ids": [message_id],
                "meta_control_message": message,
                "meta_control_semantic_message": message,
            },
        )
        records[task_id] = task
        claims.append(SimpleNamespace(
            task=task,
            entry=SimpleNamespace(
                message_id=message_id,
                session_id="recovery-session-id",
                content=message,
            ),
        ))

    claim_calls = 0

    async def _claim(*, limit: int) -> list[Any]:
        nonlocal claim_calls
        claim_calls += 1
        claimed = claims[:limit]
        del claims[:limit]
        return claimed

    async def _update(task_id: str, **fields: Any) -> None:
        record = records[task_id]
        for field, value in fields.items():
            setattr(record, field, value)

    async def _create(record: AgentTaskRecord) -> None:
        records[record.task_id] = record

    async def _get(task_id: str) -> AgentTaskRecord | None:
        return records.get(task_id)

    async def _update_context(*_args: Any, **_kwargs: Any) -> bool:
        return True

    storage = SimpleNamespace(
        claim_recoverable_meta_control_tasks=_claim,
        create_agent_task=_create,
        update_agent_task=_update,
        get_agent_task=_get,
        update_transcript_turn_context=_update_context,
    )
    first_recovery_started = asyncio.Event()
    release_first_recovery = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def _handler(run: Any) -> None:
        seen.append((run.task_id, run.queue_mode))
        if run.task_id == "recovery-task-0":
            first_recovery_started.set()
            await release_first_recovery.wait()

    runtime = TaskRuntime(
        storage=storage,
        turn_handler=_handler,
        max_concurrency=1,
        max_pending_per_session=1,
        running_heartbeat_interval_s=None,
    )
    recovery = asyncio.create_task(runtime.recover_durable_meta_controls(limit=1))
    await asyncio.wait_for(first_recovery_started.wait(), timeout=2.0)
    assert await asyncio.wait_for(recovery, timeout=2.0) == 3

    await runtime.enqueue(
        RouteEnvelope(
            source_kind=SourceKind.WEB,
            source_name="RPC",
            agent_id="main",
            session_key="agent:main:webchat:ordinary-during-recovery",
            session_id="ordinary-session-id",
            input_provenance={},
            metadata={},
        ),
        "ordinary input",
        task_id="ordinary-task",
    )
    release_first_recovery.set()
    for task_id in records:
        assert (await runtime.wait(task_id, timeout=2.0)).status == "succeeded"
    assert claim_calls == 4
    assert sorted(seen) == [
        ("ordinary-task", "followup"),
        ("recovery-task-0", "followup"),
        ("recovery-task-1", "followup"),
        ("recovery-task-2", "followup"),
    ]
    started_task_ids = [task_id for task_id, _mode in seen]
    assert started_task_ids.index("ordinary-task") < started_task_ids.index("recovery-task-2")


@pytest.mark.asyncio
async def test_meta_launch_promotes_after_durable_acceptance_before_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_id = "meta-launch-promotion-order"
    assert (
        pending_meta_launch_put(
            SESSION_KEY,
            "meta-tiny",
            client_request_id=request_id,
        )
        == "stamped"
    )

    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        order: list[str] = []
        original_accept = stack.storage.accept_turn
        original_activate = stack.runtime.activate

        async def traced_accept(*args: Any, **kwargs: Any):
            acceptance = await original_accept(*args, **kwargs)
            order.append("durable")
            return acceptance

        async def traced_activate(*args: Any, **kwargs: Any):
            order.append(
                f"activate:{pending_meta_launch_state(SESSION_KEY, client_request_id=request_id)}"
            )
            return await original_activate(*args, **kwargs)

        monkeypatch.setattr(stack.storage, "accept_turn", traced_accept)
        monkeypatch.setattr(stack.runtime, "activate", traced_activate)
        params = {
            "sessionKey": SESSION_KEY,
            "message": "/meta meta-tiny",
            "clientRequestId": request_id,
        }
        response = await get_dispatcher().dispatch(
            "rpc-meta-promotion-order",
            "chat.send",
            params,
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert order == ["durable", "activate:accepted"]
        assert pending_meta_launch_state(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "accepted"
        # Simulate the pipeline's exact one-shot claim, then replay the same
        # durable chat request. The receipt replay must not resurrect a marker.
        assert pending_meta_launch_pop(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "meta-tiny"
        replay = await get_dispatcher().dispatch(
            "rpc-meta-promotion-replay",
            "chat.send",
            params,
            stack.context,
        )
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert pending_meta_launch_state(
            SESSION_KEY,
            client_request_id=request_id,
        ) is None
        assert (
            pending_meta_launch_put(
                SESSION_KEY,
                "meta-tiny",
                client_request_id=request_id,
            )
            == "replayed"
        )


@pytest.mark.asyncio
async def test_durable_non_launch_message_does_not_promote_staged_marker(
    tmp_path: Path,
) -> None:
    request_id = "meta-launch-invalid-message"
    assert (
        pending_meta_launch_put(
            SESSION_KEY,
            "meta-tiny",
            client_request_id=request_id,
        )
        == "stamped"
    )

    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-meta-invalid-promotion",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "ordinary chat text",
                "clientRequestId": request_id,
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert pending_meta_launch_state(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "staged"
        assert pending_meta_launch_peek(
            SESSION_KEY,
            client_request_id=request_id,
        ) == "meta-tiny"

    pending_meta_launch_pop(SESSION_KEY, client_request_id=request_id)


@pytest.mark.asyncio
async def test_sessions_send_atomically_accepts_message_task_and_receipt(tmp_path: Path) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-success",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "one durable turn",
                "clientRequestId": CLIENT_REQUEST_ID,
                "clientMessageId": "composer-message-1",
                "surfaceId": "tui:atomic-test",
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["clientRequestId"] == CLIENT_REQUEST_ID
        assert response.payload["message_id"]
        assert response.payload["task_id"]
        assert response.payload["turn_id"] == response.payload["task_id"]
        assert response.payload["user_message_id"] == response.payload["message_id"]
        assert response.payload["client_message_id"] == "composer-message-1"
        assert response.payload["surface_id"] == "tui:atomic-test"
        assert response.payload["replayed"] is False
        entries = await stack.storage.get_transcript(stack.session_id)
        assert entries[0].turn_context == {
            "turn_id": response.payload["task_id"],
            "client_request_id": CLIENT_REQUEST_ID,
            "client_message_id": "composer-message-1",
            "surface_id": "tui:atomic-test",
            "intent": "send",
            "disposition": "applied",
            "revision": 1,
        }
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_pending_input_rpc_reorders_complete_queue_atomically(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "pending-input-reorder.db") as stack:
        staged_items: list[dict[str, object]] = []
        for index in range(3):
            staged = await get_dispatcher().dispatch(
                f"pending-reorder-enqueue-{index}",
                "sessions.pending_inputs.enqueue",
                {
                    "key": SESSION_KEY,
                    "pendingInputId": f"pending-reorder-{index}",
                    "clientRequestId": f"pending-reorder-request-{index}",
                    "clientMessageId": f"pending-reorder-message-{index}",
                    "message": f"queued reorder {index}",
                    "position": 10 - index,
                },
                stack.context,
            )
            assert staged.ok is True
            staged_items.append(staged.payload)

        reordered = await get_dispatcher().dispatch(
            "pending-reorder",
            "sessions.pending_inputs.reorder",
            {
                "key": SESSION_KEY,
                "items": [
                    {
                        "pendingInputId": staged_items[index]["pendingInputId"],
                        "expectedRevision": staged_items[index]["revision"],
                    }
                    for index in (2, 0, 1)
                ],
            },
            stack.context,
        )
        assert reordered.ok is True
        assert [item["pendingInputId"] for item in reordered.payload["items"]] == [
            "pending-reorder-2",
            "pending-reorder-0",
            "pending-reorder-1",
        ]
        assert [item["position"] for item in reordered.payload["items"]] == [0, 1, 2]
        assert [item["revision"] for item in reordered.payload["items"]] == [2, 2, 2]

        stale = await get_dispatcher().dispatch(
            "pending-reorder-stale",
            "sessions.pending_inputs.reorder",
            {
                "key": SESSION_KEY,
                "items": [
                    {"pendingInputId": "pending-reorder-1", "expectedRevision": 2},
                    {"pendingInputId": "pending-reorder-2", "expectedRevision": 1},
                    {"pendingInputId": "pending-reorder-0", "expectedRevision": 2},
                ],
            },
            stack.context,
        )
        assert stale.ok is False
        assert stale.error is not None
        assert stale.error.code == "PENDING_INPUT_CONFLICT"
        listed = await get_dispatcher().dispatch(
            "pending-reorder-list",
            "sessions.pending_inputs.list",
            {"key": SESSION_KEY},
            stack.context,
        )
        assert [item["pendingInputId"] for item in listed.payload["items"]] == [
            "pending-reorder-2",
            "pending-reorder-0",
            "pending-reorder-1",
        ]


@pytest.mark.asyncio
async def test_atomic_direct_send_enters_registry_admission_before_accept_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        stack.context.task_runtime = None
        registry = get_agent_task_registry()
        admission_attempted = asyncio.Event()
        accept_called = asyncio.Event()
        original_admission = registry.admission
        original_accept = stack.storage.accept_turn

        @asynccontextmanager
        async def observed_admission(session_key: str) -> AsyncIterator[None]:
            admission_attempted.set()
            async with original_admission(session_key):
                yield

        async def observed_accept(*args: Any, **kwargs: Any) -> Any:
            accept_called.set()
            return await original_accept(*args, **kwargs)

        monkeypatch.setattr(registry, "admission", observed_admission)
        monkeypatch.setattr(stack.storage, "accept_turn", observed_accept)
        admission_lock = registry._admission_locks.setdefault(
            SESSION_KEY,
            asyncio.Lock(),
        )

        async with admission_lock:
            sending = asyncio.create_task(
                get_dispatcher().dispatch(
                    "rpc-direct-admission",
                    "sessions.send",
                    {
                        "key": SESSION_KEY,
                        "message": "direct accepted turn",
                        "clientRequestId": "direct-admission-request",
                    },
                    stack.context,
                )
            )
            admission_wait = asyncio.create_task(admission_attempted.wait())
            accept_wait = asyncio.create_task(accept_called.wait())
            done, _pending = await asyncio.wait(
                {admission_wait, accept_wait},
                timeout=2.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            assert admission_wait in done
            assert accept_wait not in done
            assert sending.done() is False

        response = await asyncio.wait_for(sending, timeout=2.0)
        task = registry.get(SESSION_KEY)
        if task is not None:
            await task
        admission_wait.cancel()
        accept_wait.cancel()

        assert response.ok is True


@pytest.mark.asyncio
async def test_sessions_send_replays_same_request_without_duplicate_side_effects(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        params = {
            "key": SESSION_KEY,
            "message": "replay me exactly once",
            "clientRequestId": CLIENT_REQUEST_ID,
            "clientMessageId": "original-composer-message",
            "surfaceId": "tui:original",
        }
        first = await get_dispatcher().dispatch(
            "rpc-replay-first", "sessions.send", params, stack.context
        )
        await stack.wait_until_running()
        replay_params = {
            **params,
            "clientMessageId": "retry-composer-message",
            "surfaceId": "tui:retry",
        }
        replay = await get_dispatcher().dispatch(
            "rpc-replay-second", "sessions.send", replay_params, stack.context
        )

        assert first.ok is True
        assert replay.ok is True
        assert replay.payload["accepted"] is True
        assert replay.payload["replayed"] is True
        assert replay.payload["clientRequestId"] == CLIENT_REQUEST_ID
        assert replay.payload["message_id"] == first.payload["message_id"]
        assert replay.payload["task_id"] == first.payload["task_id"]
        assert replay.payload["turn_id"] == first.payload["turn_id"]
        assert replay.payload["client_message_id"] == "original-composer-message"
        assert replay.payload["surface_id"] == "tui:original"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_sessions_send_fast_replay_consumes_legacy_meta_launch_draft(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        params = {
            "key": SESSION_KEY,
            "message": "accepted before the legacy Meta outbox was repaired",
            "clientRequestId": CLIENT_REQUEST_ID,
            "clientMessageId": "meta-control-message",
        }
        first = await get_dispatcher().dispatch(
            "rpc-meta-draft-first", "sessions.send", params, stack.context
        )
        assert first.ok is True

        # Reproduce an older build's crash window: ingress is committed, but a
        # browser-recovery draft with the same coordinates remains durable.
        await stack.storage.stage_meta_launch_draft(
            session_key=SESSION_KEY,
            client_request_id=CLIENT_REQUEST_ID,
            meta_skill_name="meta-paper-write",
            launch_text="/meta meta-paper-write -- keep the original request",
        )
        assert await stack.storage.list_meta_launch_drafts(session_key=SESSION_KEY)

        replay = await get_dispatcher().dispatch(
            "rpc-meta-draft-replay", "sessions.send", params, stack.context
        )

        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert await stack.storage.list_meta_launch_drafts(session_key=SESSION_KEY) == []


@pytest.mark.asyncio
async def test_sessions_send_replay_exposes_terminal_task_status(tmp_path: Path) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        params = {
            "key": SESSION_KEY,
            "message": "finish before replay",
            "clientRequestId": CLIENT_REQUEST_ID,
        }
        first = await get_dispatcher().dispatch(
            "rpc-terminal-first", "sessions.send", params, stack.context
        )
        await stack.wait_until_running()
        stack.release_handler.set()
        terminal = await stack.runtime.wait(first.payload["task_id"], timeout=2.0)

        replay = await get_dispatcher().dispatch(
            "rpc-terminal-replay", "sessions.send", params, stack.context
        )

        assert str(terminal.status) == "succeeded"
        assert replay.ok is True
        assert replay.payload["replayed"] is True
        assert replay.payload["task_status"] == "succeeded"
        assert replay.payload["taskStatus"] == "succeeded"
        assert replay.payload["terminal_reason"] == "completed"
        assert replay.payload["terminal_message"] == "The task completed."


@pytest.mark.asyncio
async def test_activation_failure_is_returned_as_an_accepted_failed_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:

        async def _fail_activation(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("synthetic activation failure")

        stack.runtime.activate = _fail_activation  # type: ignore[method-assign]
        response = await get_dispatcher().dispatch(
            "rpc-activation-failure",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "accepted before activation fails",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["task_status"] == "failed"
        assert response.payload["terminal_reason"] == "activation_failed"
        assert response.payload["terminal_message"] == "The task failed before it could finish."
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None
        assert task.status == "failed"
        assert stack.runtime._reservations_by_session == {}
        assert stack.runtime._tasks == {}


@pytest.mark.asyncio
async def test_post_accept_notification_failure_does_not_reject_the_turn(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:

        def _fail_notification(_entry: Any) -> None:
            raise RuntimeError("synthetic post-accept notification failure")

        stack.manager.notify_message_appended = _fail_notification  # type: ignore[method-assign]
        response = await get_dispatcher().dispatch(
            "rpc-post-accept-failure",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "durable despite observer failure",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["task_status"] == "queued"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_cancellation_after_commit_still_activates_the_durable_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        original_accept_turn = stack.storage.accept_turn
        committed = asyncio.Event()
        release_accept = asyncio.Event()

        async def _pause_after_commit(*args: Any, **kwargs: Any) -> Any:
            result = await original_accept_turn(*args, **kwargs)
            committed.set()
            await release_accept.wait()
            return result

        stack.storage.accept_turn = _pause_after_commit  # type: ignore[method-assign]
        request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-cancel-after-commit",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "commit and activate despite disconnect",
                    "clientRequestId": CLIENT_REQUEST_ID,
                },
                stack.context,
            )
        )
        await asyncio.wait_for(committed.wait(), timeout=2.0)

        request.cancel()
        await asyncio.sleep(0)
        release_accept.set()
        response = await asyncio.wait_for(request, timeout=2.0)
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["task_id"] in stack.runtime._tasks
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }
        assert stack.runtime._reservations_by_session == {}


@pytest.mark.asyncio
async def test_sessions_send_rejects_request_id_reuse_with_different_payload(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        first = await get_dispatcher().dispatch(
            "rpc-conflict-first",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "original payload",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )
        await stack.wait_until_running()
        conflict = await get_dispatcher().dispatch(
            "rpc-conflict-second",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "different payload",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )

        assert first.ok is True
        assert conflict.ok is False
        assert conflict.error is not None
        assert conflict.error.code == "IDEMPOTENCY_CONFLICT"
        assert conflict.error.retryable is False
        assert conflict.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }


@pytest.mark.asyncio
async def test_sessions_send_storage_busy_is_retryable_and_has_no_acceptance_side_effects(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(
            stack.db_path,
            isolation_level=None,
            timeout=0.0,
        )
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            response = await get_dispatcher().dispatch(
                "rpc-busy",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "must remain unaccepted",
                    "clientRequestId": CLIENT_REQUEST_ID,
                },
                stack.context,
            )

            assert response.ok is False
            assert response.error is not None
            assert response.error.code == "STORAGE_BUSY"
            assert response.error.retryable is True
            assert response.error.accepted is False
            assert response.error.retry_after_ms is not None
            assert _table_counts(stack.db_path) == {
                "transcript_entries": 0,
                "agent_tasks": 0,
                "turn_ingress_receipts": 0,
            }
            _assert_no_runtime_acceptance_state(stack.runtime)
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()


@pytest.mark.asyncio
async def test_sessions_send_stale_epoch_is_retryable_and_unaccepted(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        original_accept_turn = stack.storage.accept_turn

        async def _advance_epoch_before_accept(*args: Any, **kwargs: Any) -> Any:
            await stack.storage.increment_epoch(SESSION_KEY)
            return await original_accept_turn(*args, **kwargs)

        stack.storage.accept_turn = _advance_epoch_before_accept  # type: ignore[method-assign]
        response = await get_dispatcher().dispatch(
            "rpc-stale-epoch",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "retry after reset",
                "clientRequestId": CLIENT_REQUEST_ID,
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "SESSION_CHANGED"
        assert response.error.retryable is True
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        _assert_no_runtime_acceptance_state(stack.runtime)


@pytest.mark.asyncio
async def test_sessions_send_queue_full_is_unaccepted_and_does_not_persist_message(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(
        tmp_path / "sessions.db",
        max_pending_per_session=1,
    ) as stack:
        blocker = await stack.runtime.reserve(
            RouteEnvelope(
                source_kind=SourceKind.WEB,
                source_name="queue-capacity-test",
                agent_id="main",
                session_key=SESSION_KEY,
                input_provenance={"kind": "synthetic-test"},
            ),
            "reserve the only queue slot",
        )
        response = await get_dispatcher().dispatch(
            "rpc-queue-full",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "must not be persisted",
                "clientRequestId": CLIENT_REQUEST_ID,
                "queueMode": "followup",
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "QUEUE_FULL"
        assert response.error.retryable is True
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        assert stack.runtime._tasks == {}
        assert stack.runtime._pending_by_session == {}
        assert stack.runtime._reservations_by_session == {SESSION_KEY: [blocker]}

        await stack.runtime.abort_reservation(blocker)
        _assert_no_runtime_acceptance_state(stack.runtime)


@pytest.mark.asyncio
async def test_sessions_send_during_shutdown_is_unavailable_without_persistence(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        result = await stack.runtime.shutdown(timeout=1.0)
        assert result.clean is True

        response = await get_dispatcher().dispatch(
            "rpc-runtime-shutting-down",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "must not be persisted",
                "clientRequestId": CLIENT_REQUEST_ID,
                "queueMode": "followup",
            },
            stack.context,
        )

        assert response.ok is False
        assert response.error is not None
        assert response.error.code == "UNAVAILABLE"
        assert response.error.retryable is True
        assert response.error.accepted is False
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 0,
            "agent_tasks": 0,
            "turn_ingress_receipts": 0,
        }
        _assert_no_runtime_acceptance_state(stack.runtime)


@pytest.mark.asyncio
async def test_collect_mode_atomically_merges_message_and_receipt_into_queued_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        blocker = await get_dispatcher().dispatch(
            "rpc-collect-blocker",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "running blocker",
                "clientRequestId": "collect-blocker",
            },
            stack.context,
        )
        await stack.wait_until_running()
        first = await get_dispatcher().dispatch(
            "rpc-collect-first",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "collect one",
                "queueMode": "collect",
                "clientRequestId": "collect-first",
                "clientMessageId": "collect-composer-1",
                "surfaceId": "tui:collect-1",
            },
            stack.context,
        )
        second = await get_dispatcher().dispatch(
            "rpc-collect-second",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "collect two",
                "queueMode": "collect",
                "clientRequestId": "collect-second",
                "clientMessageId": "collect-composer-2",
                "surfaceId": "tui:collect-2",
            },
            stack.context,
        )

        assert blocker.ok is True
        assert first.ok is True
        assert second.ok is True
        assert second.payload["task_id"] == first.payload["task_id"]
        assert first.payload["turn_id"] == first.payload["task_id"]
        assert second.payload["turn_id"] == first.payload["task_id"]
        assert second.payload["client_message_id"] == "collect-composer-2"
        assert second.payload["surface_id"] == "tui:collect-2"
        candidate = stack.runtime._tasks[first.payload["task_id"]]
        assert candidate.message == "collect one\ncollect two"
        persisted = await stack.storage.get_agent_task(first.payload["task_id"])
        assert persisted is not None
        assert persisted.details is not None
        assert persisted.details["collected"] is True
        assert persisted.details["message_count"] == 2
        entries = await stack.storage.get_transcript(stack.session_id)
        assert entries[-2].turn_context == {
            "turn_id": first.payload["task_id"],
            "client_request_id": "collect-first",
            "client_message_id": "collect-composer-1",
            "surface_id": "tui:collect-1",
            "intent": "send",
            "disposition": "queued",
            "revision": 1,
            "sandbox_mode_resolution": {
                "desiredMode": "full",
                "effectiveMode": "full",
                "fallbackReason": None,
                "confirmationRequired": False,
            },
        }
        assert entries[-1].turn_context == {
            "turn_id": first.payload["task_id"],
            "client_request_id": "collect-second",
            "client_message_id": "collect-composer-2",
            "surface_id": "tui:collect-2",
            "intent": "send",
            "disposition": "queued",
            "target_turn_id": first.payload["task_id"],
            "revision": 2,
            "sandbox_mode_resolution": {
                "desiredMode": "full",
                "effectiveMode": "full",
                "fallbackReason": None,
                "confirmationRequired": False,
            },
        }
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 3,
            "agent_tasks": 2,
            "turn_ingress_receipts": 3,
        }


@pytest.mark.asyncio
async def test_concurrent_first_collects_share_one_admission_and_task(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        blocker = await get_dispatcher().dispatch(
            "rpc-concurrent-collect-blocker",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "running blocker",
                "clientRequestId": "concurrent-collect-blocker",
            },
            stack.context,
        )
        await stack.wait_until_running()

        original_reserve = stack.runtime.reserve
        first_reserved = asyncio.Event()
        release_first = asyncio.Event()
        pause_next_collect = True

        async def _pause_first_collect_reservation(
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            nonlocal pause_next_collect
            reservation = await original_reserve(*args, **kwargs)
            if kwargs.get("mode") == "collect" and pause_next_collect:
                pause_next_collect = False
                first_reserved.set()
                await release_first.wait()
            return reservation

        stack.runtime.reserve = _pause_first_collect_reservation  # type: ignore[method-assign]
        first_request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-concurrent-collect-first",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "collect first",
                    "queueMode": "collect",
                    "clientRequestId": "concurrent-collect-first",
                },
                stack.context,
            )
        )
        await asyncio.wait_for(first_reserved.wait(), timeout=2.0)
        second_request = asyncio.create_task(
            get_dispatcher().dispatch(
                "rpc-concurrent-collect-second",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "collect second",
                    "queueMode": "collect",
                    "clientRequestId": "concurrent-collect-second",
                },
                stack.context,
            )
        )
        await asyncio.sleep(0.05)

        assert second_request.done() is False
        release_first.set()
        first, second = await asyncio.gather(first_request, second_request)

        assert blocker.ok is True
        assert first.ok is True
        assert second.ok is True
        assert first.payload["task_id"] == second.payload["task_id"]
        candidate = stack.runtime._tasks[first.payload["task_id"]]
        assert candidate.message == "collect first\ncollect second"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 3,
            "agent_tasks": 2,
            "turn_ingress_receipts": 3,
        }


@pytest.mark.asyncio
async def test_collect_storage_busy_leaves_transcript_receipt_and_candidate_unchanged(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        await get_dispatcher().dispatch(
            "rpc-collect-busy-blocker",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "running blocker",
                "clientRequestId": "collect-busy-blocker",
            },
            stack.context,
        )
        await stack.wait_until_running()
        queued = await get_dispatcher().dispatch(
            "rpc-collect-busy-first",
            "sessions.send",
            {
                "key": SESSION_KEY,
                "message": "collect before busy",
                "queueMode": "collect",
                "clientRequestId": "collect-busy-first",
            },
            stack.context,
        )
        candidate = stack.runtime._tasks[queued.payload["task_id"]]
        original_message = candidate.message
        original_details = (await stack.storage.get_agent_task(queued.payload["task_id"])).details

        stack.storage._busy_budget_seconds = 0.0
        await stack.storage.conn.execute("PRAGMA busy_timeout = 0")
        external_writer = sqlite3.connect(
            stack.db_path,
            isolation_level=None,
            timeout=0.0,
        )
        external_writer.execute("BEGIN IMMEDIATE")
        try:
            rejected = await get_dispatcher().dispatch(
                "rpc-collect-busy-second",
                "sessions.send",
                {
                    "key": SESSION_KEY,
                    "message": "must not be collected",
                    "queueMode": "collect",
                    "clientRequestId": "collect-busy-second",
                },
                stack.context,
            )

            assert rejected.ok is False
            assert rejected.error is not None
            assert rejected.error.code == "STORAGE_BUSY"
            assert rejected.error.accepted is False
            assert candidate.message == original_message
            persisted = await stack.storage.get_agent_task(queued.payload["task_id"])
            assert persisted is not None
            assert persisted.details == original_details
            assert _table_counts(stack.db_path) == {
                "transcript_entries": 2,
                "agent_tasks": 2,
                "turn_ingress_receipts": 2,
            }
        finally:
            external_writer.execute("ROLLBACK")
            external_writer.close()


@pytest.mark.asyncio
async def test_chat_send_forwards_client_request_id_into_atomic_acceptance(
    tmp_path: Path,
) -> None:
    async with _open_real_stack(tmp_path / "sessions.db") as stack:
        response = await get_dispatcher().dispatch(
            "rpc-chat-forward",
            "chat.send",
            {
                "sessionKey": SESSION_KEY,
                "message": "forward this request identity",
                "queueMode": "steer",
                "clientRequestId": CLIENT_REQUEST_ID,
                "clientMessageId": "web-composer-message",
                "surfaceId": "webui:chat",
            },
            stack.context,
        )
        await stack.wait_until_running()

        assert response.ok is True
        assert response.payload["accepted"] is True
        assert response.payload["clientRequestId"] == CLIENT_REQUEST_ID
        assert response.payload["client_message_id"] == "web-composer-message"
        assert response.payload["surface_id"] == "webui:chat"
        acceptance = await stack.storage.get_turn_ingress_receipt(
            source_scope="web:webchat:operator",
            request_session_key=SESSION_KEY,
            client_request_id=CLIENT_REQUEST_ID,
        )
        assert acceptance is not None
        assert acceptance.receipt.client_request_id == CLIENT_REQUEST_ID
        assert acceptance.receipt.message_id == response.payload["message_id"]
        assert acceptance.receipt.task_id == response.payload["task_id"]
        task = await stack.storage.get_agent_task(response.payload["task_id"])
        assert task is not None
        assert task.queue_mode == "interrupt"
        entries = await stack.storage.get_transcript(stack.session_id)
        assert entries[0].turn_context is not None
        assert entries[0].turn_context["client_request_id"] == CLIENT_REQUEST_ID
        assert entries[0].turn_context["client_message_id"] == "web-composer-message"
        assert entries[0].turn_context["surface_id"] == "webui:chat"
        assert _table_counts(stack.db_path) == {
            "transcript_entries": 1,
            "agent_tasks": 1,
            "turn_ingress_receipts": 1,
        }
