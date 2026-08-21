"""Restricted semantic document tool contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    AnchorKind,
    AnchorState,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactMutationAttemptController,
    ArtifactNotFoundError,
    ArtifactSessionService,
    ChangeSetStatus,
    MutationAttemptStatus,
    PreparedPromptAnnotationTarget,
    consume_prepared_prompt_annotations_on_conn,
    consume_prompt_annotations_on_conn,
)
from opensquilla.artifacts import ArtifactStore
from opensquilla.engine.types import ToolCall
from opensquilla.gateway.artifact_contexts import (
    DOCUMENT_CONTEXT_TOOL_NAMES,
    BoundDocumentContext,
    BoundPromptAnnotationContext,
    BoundPromptAnnotationTarget,
)
from opensquilla.tools import get_default_registry
from opensquilla.tools.builtin.artifact_editing import _range_error, _stale_context
from opensquilla.tools.builtin.artifact_range_grants import ArtifactRangeGrantError
from opensquilla.tools.builtin.document_format_adapters import DocumentMutationError
from opensquilla.tools.dispatch import build_tool_handler
from opensquilla.tools.types import CallerKind, InteractionMode, ToolContext

SESSION_KEY = "agent:main:webchat:document"
SESSION_ID = "session-document"
TURN_ID = "turn-document"
DOCUMENT_TOOL_NAMES = frozenset(
    {"document_apply", "document_inspect", "document_locate", "document_read"}
)
RETIRED_ANNOTATION_TOOL_NAMES = frozenset(
    {
        "artifact_get_context",
        "artifact_read_annotations",
        "artifact_read_selection",
        "artifact_read_structure",
        "artifact_validate",
        "html_edit_source",
        "html_locate_source",
        "html_read_source",
        "html_search_source",
    }
)


async def _bound_document_context(tmp_path: Path, *, source: bytes | None = None):
    service = await ArtifactSessionService.open(tmp_path / "bound-document.db")
    store = ArtifactStore(tmp_path / "bound-document-media")
    payload = source or b"<main><h1>Original heading</h1><p>Keep me</p></main>"
    ref = store.publish_bytes(
        payload,
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        name="bound.html",
        mime="text/html",
        source="bound_document_test",
    )
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="Bound page",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "owner"),
    )
    bound = BoundDocumentContext(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        artifact_format="html",
        tool_names=DOCUMENT_CONTEXT_TOOL_NAMES,
        operation_class="document_edit",
        request_context_prompt="Bound current document.",
    )
    controller = ArtifactMutationAttemptController(
        service,
        document_id=created.document.document_id,
        base_revision_id=created.revision.revision_id,
        turn_id=TURN_ID,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        interaction_mode=InteractionMode.INTERACTIVE,
        subagent_depth=0,
        agent_id="artifact-agent",
        session_key=SESSION_KEY,
        task_id=TURN_ID,
        artifact_media_root=str(tmp_path / "bound-document-media"),
        artifact_session_id=SESSION_ID,
        surfaced_tools=set(DOCUMENT_CONTEXT_TOOL_NAMES),
        artifact_context=bound,
        artifact_session=service,
        artifact_mutation_attempt_controller=controller,
    )
    return service, store, payload, ref, created, ctx


async def _sent_img_context(tmp_path: Path):
    service = await ArtifactSessionService.open(tmp_path / "document.db")
    store = ArtifactStore(tmp_path / "media")
    source = b'<main><img id="hero" src="photo.png"><p>Keep me</p></main>'
    ref = store.publish_bytes(
        source,
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        name="page.html",
        mime="text/html",
        source="document_test",
    )
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="Page",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "owner"),
    )
    opening_start = source.decode().index("<img")
    opening_end = source.decode().index(">", opening_start) + 1
    anchor = await service.create_anchor(
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        kind=AnchorKind.DOM_SOURCE,
        locator={
            "start_offset": opening_start,
            "start_tag_end_offset": opening_end,
            "tag_name": "img",
            "source_sha256": created.revision.artifact_sha256,
        },
        quote='<img id="hero" src="photo.png">',
        context={"before": "<main>", "after": "<p>Keep me</p>"},
        actor=Actor(ActorKind.USER, "owner"),
    )
    draft = await service.create_prompt_annotation(
        annotation_id="annotation-remove-img",
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        anchor_id=anchor.anchor_id,
        body="Remove this image.",
    )
    preflighted = await service.preflight_prompt_annotations(
        annotation_ids=(draft.annotation_id,),
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
    )
    async with service.repository._transaction("test.document.consume") as conn:
        await consume_prompt_annotations_on_conn(
            conn,
            expected_annotations=preflighted,
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
            message_id="message-document",
            turn_id=TURN_ID,
            updated_at=1234,
        )
    snapshot: dict[str, object] = {
        "version": 1,
        "annotationId": draft.annotation_id,
        "order": 0,
        "body": draft.body,
        "document": {
            "id": created.document.document_id,
            "name": created.document.name,
            "kind": created.document.kind.value,
        },
        "revision": {
            "id": created.revision.revision_id,
            "generation": created.revision.generation,
            "sha256": created.revision.artifact_sha256,
        },
        "anchor": {
            "id": anchor.anchor_id,
            "kind": anchor.kind.value,
            "tagName": "img",
            "locator": anchor.locator,
            "quote": anchor.quote,
        },
    }
    request_context = "Bound annotated document context."
    bound = BoundPromptAnnotationContext(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        annotation_ids=(draft.annotation_id,),
        anchor_ids=(anchor.anchor_id,),
        snapshots=(snapshot,),
        artifact_format="html",
        tool_names=DOCUMENT_TOOL_NAMES,
        operation_class="selection_edit",
        request_context_prompt=request_context,
    )
    controller = ArtifactMutationAttemptController(
        service,
        document_id=created.document.document_id,
        base_revision_id=created.revision.revision_id,
        turn_id=TURN_ID,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        interaction_mode=InteractionMode.INTERACTIVE,
        subagent_depth=0,
        agent_id="artifact-agent",
        session_key=SESSION_KEY,
        task_id=TURN_ID,
        artifact_media_root=str(tmp_path / "media"),
        artifact_session_id=SESSION_ID,
        surfaced_tools=set(DOCUMENT_TOOL_NAMES),
        allowed_tools=set(DOCUMENT_TOOL_NAMES),
        exclusive_tools=set(DOCUMENT_TOOL_NAMES),
        artifact_context=bound,
        artifact_session=service,
        artifact_mutation_attempt_controller=controller,
    )
    return service, store, source, ref, created, anchor, ctx


async def _sent_heading_and_img_context(tmp_path: Path):
    service = await ArtifactSessionService.open(tmp_path / "document-batch.db")
    store = ArtifactStore(tmp_path / "media-batch")
    source = (
        b'<style>.title{color:red}</style><main><h1 class="title">Original heading</h1>'
        b'<img id="hero" src="photo.png"><p>Keep me</p></main>'
    )
    ref = store.publish_bytes(
        source,
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        name="batch.html",
        mime="text/html",
        source="document_batch_test",
    )
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="Batch page",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "owner"),
    )
    source_text = source.decode()
    selections = (
        (
            "annotation-replace-heading",
            "Replace the selected heading.",
            "h1",
            '<h1 class="title">',
        ),
        (
            "annotation-remove-batch-img",
            "Remove the selected image.",
            "img",
            '<img id="hero" src="photo.png">',
        ),
    )
    anchors = []
    drafts = []
    for annotation_id, body, tag_name, opening_tag in selections:
        opening_start = source_text.index(opening_tag)
        opening_end = opening_start + len(opening_tag)
        anchor = await service.create_anchor(
            document_id=created.document.document_id,
            revision_id=created.revision.revision_id,
            kind=AnchorKind.DOM_SOURCE,
            locator={
                "start_offset": opening_start,
                "start_tag_end_offset": opening_end,
                "tag_name": tag_name,
                "source_sha256": created.revision.artifact_sha256,
            },
            quote=opening_tag,
            context={"before": source_text[:opening_start], "after": source_text[opening_end:]},
            actor=Actor(ActorKind.USER, "owner"),
        )
        anchors.append(anchor)
        drafts.append(
            await service.create_prompt_annotation(
                annotation_id=annotation_id,
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
                session_epoch=0,
                document_id=created.document.document_id,
                revision_id=created.revision.revision_id,
                anchor_id=anchor.anchor_id,
                body=body,
            )
        )
    preflighted = await service.preflight_prompt_annotations(
        annotation_ids=tuple(draft.annotation_id for draft in drafts),
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
    )
    batch_turn_id = f"{TURN_ID}-batch"
    async with service.repository._transaction("test.document.consume-batch") as conn:
        await consume_prompt_annotations_on_conn(
            conn,
            expected_annotations=preflighted,
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
            message_id="message-document-batch",
            turn_id=batch_turn_id,
            updated_at=1234,
        )
    snapshots = tuple(
        {
            "version": 1,
            "annotationId": draft.annotation_id,
            "order": order,
            "body": draft.body,
            "document": {
                "id": created.document.document_id,
                "name": created.document.name,
                "kind": created.document.kind.value,
            },
            "revision": {
                "id": created.revision.revision_id,
                "generation": created.revision.generation,
                "sha256": created.revision.artifact_sha256,
            },
            "anchor": {
                "id": anchor.anchor_id,
                "kind": anchor.kind.value,
                "tagName": anchor.locator["tag_name"],
                "locator": anchor.locator,
                "quote": anchor.quote,
            },
        }
        for order, (draft, anchor) in enumerate(zip(drafts, anchors, strict=True))
    )
    request_context = "Bound annotated document context."
    bound = BoundPromptAnnotationContext(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        annotation_ids=tuple(draft.annotation_id for draft in drafts),
        anchor_ids=tuple(anchor.anchor_id for anchor in anchors),
        snapshots=snapshots,
        artifact_format="html",
        tool_names=DOCUMENT_TOOL_NAMES,
        operation_class="selection_edit",
        request_context_prompt=request_context,
    )
    controller = ArtifactMutationAttemptController(
        service,
        document_id=created.document.document_id,
        base_revision_id=created.revision.revision_id,
        turn_id=batch_turn_id,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        interaction_mode=InteractionMode.INTERACTIVE,
        subagent_depth=0,
        agent_id="artifact-agent",
        session_key=SESSION_KEY,
        task_id=batch_turn_id,
        artifact_media_root=str(tmp_path / "media-batch"),
        artifact_session_id=SESSION_ID,
        surfaced_tools=set(DOCUMENT_TOOL_NAMES),
        allowed_tools=set(DOCUMENT_TOOL_NAMES),
        exclusive_tools=set(DOCUMENT_TOOL_NAMES),
        artifact_context=bound,
        artifact_session=service,
        artifact_mutation_attempt_controller=controller,
    )
    return service, store, source, ref, created, ctx


async def _sent_contextual_button_context(
    tmp_path: Path,
    *,
    source_text: str | None = None,
):
    service = await ArtifactSessionService.open(tmp_path / "contextual-document.db")
    store = ArtifactStore(tmp_path / "contextual-media")
    source = (
        source_text
        or '<main><button id="one">One</button><button id="two">Two</button></main>'
    ).encode()
    ref = store.publish_bytes(
        source,
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        name="contextual.html",
        mime="text/html",
        source="contextual_document_test",
    )
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="Contextual page",
        kind=ArtifactKind.HTML,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(ActorKind.USER, "owner"),
    )
    original_opening = '<button id="one">'
    original_start = source.decode().index(original_opening)
    original = await service.create_anchor(
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        kind=AnchorKind.DOM_SOURCE,
        locator={
            "start_offset": original_start,
            "start_tag_end_offset": original_start + len(original_opening),
            "tag_name": "button",
            "source_sha256": ref.sha256,
        },
        quote=original_opening,
        context={"semantic_profile_v1": {"version": 1, "tag_name": "button"}},
        actor=Actor(ActorKind.USER, "owner"),
    )
    draft = await service.create_prompt_annotation(
        annotation_id="annotation-contextual-button",
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        session_epoch=0,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        anchor_id=original.anchor_id,
        body="Remove the intended button.",
    )
    prepared = PreparedPromptAnnotationTarget(
        expected_annotation=draft,
        previous_anchor_id=original.anchor_id,
        anchor_id="anchor-contextual-button",
        audit_event_id="audit-contextual-button",
        revision_id=created.revision.revision_id,
        kind=AnchorKind.DOM_SOURCE,
        locator={
            "tag_name": "button",
            "source_sha256": ref.sha256,
            "offset_encoding": "unicode-code-point",
        },
        quote=original_opening,
        context={
            "semantic_profile_v1": {"version": 1, "tag_name": "button"},
            "target_reason": "ambiguous",
        },
        state=AnchorState.ORPHANED,
        actor_kind=ActorKind.USER,
        actor_id="owner",
    )
    async with service.repository._transaction("test.contextual.consume") as conn:
        await consume_prepared_prompt_annotations_on_conn(
            conn,
            prepared_targets=(prepared,),
            session_key=SESSION_KEY,
            session_id=SESSION_ID,
            session_epoch=0,
            message_id="message-contextual",
            turn_id=TURN_ID,
            updated_at=1234,
        )
    snapshot = {
        "version": 1,
        "annotationId": draft.annotation_id,
        "order": 0,
        "body": draft.body,
        "targetStatus": "contextual",
        "targetReason": "ambiguous",
        "targetKind": "button",
        "targetText": None,
        "document": {
            "id": created.document.document_id,
            "name": created.document.name,
            "kind": created.document.kind.value,
        },
        "revision": {
            "id": created.revision.revision_id,
            "generation": created.revision.generation,
            "sha256": created.revision.artifact_sha256,
        },
        "anchor": {
            "id": prepared.anchor_id,
            "kind": prepared.kind.value,
            "tagName": "button",
            "locator": prepared.locator,
            "quote": prepared.quote,
        },
    }
    bound = BoundPromptAnnotationContext(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        targets=(
            BoundPromptAnnotationTarget(
                annotation_id=draft.annotation_id,
                anchor_id=prepared.anchor_id,
                status="contextual",
                reason="ambiguous",
                tag_name="button",
                target_kind="button",
                target_text=None,
            ),
        ),
        snapshots=(snapshot,),
        artifact_format="html",
        tool_names=DOCUMENT_TOOL_NAMES,
        operation_class="selection_edit",
        request_context_prompt="Bound contextual document context.",
    )
    controller = ArtifactMutationAttemptController(
        service,
        document_id=created.document.document_id,
        base_revision_id=created.revision.revision_id,
        turn_id=TURN_ID,
    )
    ctx = ToolContext(
        is_owner=True,
        caller_kind=CallerKind.WEB,
        interaction_mode=InteractionMode.INTERACTIVE,
        subagent_depth=0,
        agent_id="artifact-agent",
        session_key=SESSION_KEY,
        task_id=TURN_ID,
        artifact_media_root=str(tmp_path / "contextual-media"),
        artifact_session_id=SESSION_ID,
        surfaced_tools=set(DOCUMENT_TOOL_NAMES),
        allowed_tools=set(DOCUMENT_TOOL_NAMES),
        exclusive_tools=set(DOCUMENT_TOOL_NAMES),
        artifact_context=bound,
        artifact_session=service,
        artifact_mutation_attempt_controller=controller,
    )
    return service, store, source, ref, created, ctx


async def _call(
    handler,
    name: str,
    arguments: dict[str, object],
    *,
    tool_use_id: str | None = None,
):
    return await handler(
        ToolCall(
            tool_use_id=tool_use_id or f"call-{name}",
            tool_name=name,
            arguments=arguments,
        )
    )


def test_restricted_document_toolset_is_exactly_four_tools() -> None:
    registry = get_default_registry()
    assert DOCUMENT_TOOL_NAMES == {
        "document_apply",
        "document_inspect",
        "document_locate",
        "document_read",
    }
    for name in DOCUMENT_TOOL_NAMES:
        registered = registry.get(name)
        assert registered is not None
        assert registered.spec.owner_only is True
        assert registered.spec.exposed_by_default is False
    for name in RETIRED_ANNOTATION_TOOL_NAMES:
        assert registry.get(name) is None
    writer = registry.get("document_apply")
    assert writer is not None
    assert writer.spec.terminates_turn is False
    assert writer.spec.terminal_response_field is None
    assert set(writer.spec.parameters["properties"]) == {"mutations"}
    mutation_schema = writer.spec.parameters["properties"]["mutations"]["items"]
    assert set(mutation_schema["properties"]) == {"grant_token", "input"}

    locator = registry.get("document_locate")
    assert locator is not None
    locator_properties = locator.spec.parameters["properties"]
    assert "Reuse a matching ready-target" in locator_properties["operation"]["description"]
    assert "Only for a contextual selection" in locator_properties["candidateSource"][
        "description"
    ]
    assert "Omit for every ready selection" in locator_properties["candidateSource"][
        "description"
    ]

    patch_writer = registry.get("document_patch")
    assert patch_writer is not None
    assert patch_writer.spec.owner_only is True
    assert patch_writer.spec.exposed_by_default is False
    assert set(patch_writer.spec.parameters["properties"]) == {
        "edits",
        "expectedSha256",
    }
    edit_schema = patch_writer.spec.parameters["properties"]["edits"]["items"]
    assert set(edit_schema["properties"]) == {"expectedText", "replacement"}


@pytest.mark.asyncio
async def test_bound_document_patch_commits_unique_text_edits_atomically(
    tmp_path: Path,
) -> None:
    service, store, _source, ref, created, ctx = await _bound_document_context(tmp_path)
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        read = await _call(handler, "document_read", {"view": "source"})
        assert read.is_error is False, read.content
        assert json.loads(read.content)["sha256"] == ref.sha256

        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-document_patch")
        applied = await _call(
            handler,
            "document_patch",
            {
                "expectedSha256": ref.sha256,
                "edits": [
                    {
                        "expectedText": "Original heading",
                        "replacement": "Updated heading",
                    },
                    {
                        "expectedText": "Keep me",
                        "replacement": "Still here",
                    },
                ],
            },
        )

        assert applied.is_error is False, applied.content
        result = json.loads(applied.content)
        assert result["status"] == "applied"
        assert result["change_set"]["mutation_kind"] == "document_text_patch"
        assert result["change_set"]["patch_count"] == 2
        document = await service.get_document(created.document.document_id)
        revision = await service.get_revision(document.head_revision_id)
        _new_ref, path = store.resolve_for_download(
            revision.artifact_id,
            session_id=SESSION_ID,
        )
        assert path.read_bytes() == (
            b"<main><h1>Updated heading</h1><p>Still here</p></main>"
        )
        change_sets = await service.list_change_sets(created.document.document_id)
        assert len(change_sets) == 1
        assert change_sets[0].status is ChangeSetStatus.APPLIED
        assert change_sets[0].operations[0]["op"] == "document_text_patch"
        assert len(await service.list_revisions(created.document.document_id)) == 2
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=TURN_ID,
            tool_use_id="call-document_patch",
        )
        assert receipt.status is MutationAttemptStatus.APPLIED
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_bound_document_read_uses_call_time_head_after_context_binding(
    tmp_path: Path,
) -> None:
    service, store, _source, _ref, created, ctx = await _bound_document_context(tmp_path)
    updated_source = b"<main><h1>Current heading</h1></main>"
    updated_ref = store.publish_bytes(
        updated_source,
        session_id=SESSION_ID,
        session_key=SESSION_KEY,
        name="bound.html",
        mime="text/html",
        source="bound_document_current_head_test",
    )
    committed = await service.commit_revision(
        document_id=created.document.document_id,
        expected_head_revision_id=created.revision.revision_id,
        expected_state_revision=created.document.state_revision,
        artifact=ArtifactBlobRef(
            artifact_id=updated_ref.id,
            sha256=updated_ref.sha256,
            filename=updated_ref.name,
            media_type=updated_ref.mime,
            byte_size=updated_ref.size,
        ),
        actor=Actor(ActorKind.USER, "owner"),
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        read = await _call(handler, "document_read", {"view": "source"})

        assert read.is_error is False, read.content
        payload = json.loads(read.content)
        assert payload["sha256"] == committed.revision.artifact_sha256
        assert payload["chunk"]["text"] == updated_source.decode("utf-8")
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", ["", " \t\r\n"])
async def test_bound_document_first_read_accepts_provider_required_empty_cursor(
    tmp_path: Path,
    cursor: str,
) -> None:
    service, _store, source, ref, _created, ctx = await _bound_document_context(tmp_path)
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        read = await _call(
            handler,
            "document_read",
            {"view": "source", "cursor": cursor},
        )

        assert read.is_error is False, read.content
        payload = json.loads(read.content)
        assert payload["sha256"] == ref.sha256
        assert payload["chunk"]["text"] == source.decode("utf-8")
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_bound_document_read_bounds_unissued_cursor_retries_without_blocking_valid_cursor(
    tmp_path: Path,
) -> None:
    source = b"<main>" + (b"x" * 700) + b"</main>"
    service, _store, _source, _ref, _created, ctx = await _bound_document_context(
        tmp_path,
        source=source,
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        first = await _call(
            handler,
            "document_read",
            {"view": "source", "cursor": "", "max_chars": 256},
        )
        assert first.is_error is False, first.content
        next_cursor = json.loads(first.content)["nextCursor"]
        assert isinstance(next_cursor, str)

        for suffix in ("a", "b"):
            invalid = await _call(
                handler,
                "document_read",
                {"view": "source", "cursor": "hcur_" + (suffix * 43)},
            )
            assert invalid.is_error is True
            assert "ARTIFACT_CURSOR_INVALID" in invalid.content

        continued = await _call(
            handler,
            "document_read",
            {"view": "source", "cursor": next_cursor, "max_chars": 256},
        )
        assert continued.is_error is False, continued.content

        stopped = await _call(
            handler,
            "document_read",
            {"view": "source", "cursor": "hcur_" + ("c" * 43)},
        )
        assert stopped.is_error is True
        assert "ARTIFACT_RANGE_QUERY_LIMIT" in stopped.content
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "edits", "expected_code"),
    [
        (
            b"<p>same</p><p>same</p>",
            [{"expectedText": "same", "replacement": "changed"}],
            "DOCUMENT_PATCH_TEXT_AMBIGUOUS",
        ),
        (
            b"<p>abcdef</p>",
            [
                {"expectedText": "abcdef", "replacement": "one"},
                {"expectedText": "cde", "replacement": "two"},
            ],
            "DOCUMENT_PATCH_INVALID",
        ),
        (
            b"<p>abcdef</p>",
            [{"expectedText": "", "replacement": "changed"}],
            "DOCUMENT_PATCH_EXPECTED_TEXT_INVALID",
        ),
    ],
    ids=["ambiguous", "overlap", "empty"],
)
async def test_bound_document_patch_rejects_unsafe_text_matches(
    tmp_path: Path,
    source: bytes,
    edits: list[dict[str, str]],
    expected_code: str,
) -> None:
    service, _store, _source, ref, created, ctx = await _bound_document_context(
        tmp_path,
        source=source,
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-document_patch")
        result = await _call(
            handler,
            "document_patch",
            {"expectedSha256": ref.sha256, "edits": edits},
        )
        assert result.is_error is True
        assert expected_code in result.content
        assert await service.list_change_sets(created.document.document_id) == ()
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.parametrize(
    ("code", "retry_policy"),
    [
        ("ARTIFACT_RANGE_TOKEN_INVALID", "forbidden"),
        ("ARTIFACT_RANGE_TOKEN_USED", "refresh"),
        ("ARTIFACT_RANGE_STALE", "refresh"),
        ("ARTIFACT_RANGE_OVERLAP", "correctable"),
        ("ARTIFACT_RANGE_LIMIT", "forbidden"),
    ],
)
def test_range_grant_errors_have_stable_agent_retry_policy(
    code: str,
    retry_policy: str,
) -> None:
    error = _range_error(ArtifactRangeGrantError(code, "Safe detail."))
    assert isinstance(error, DocumentMutationError)
    assert error.code == code
    assert error.retry_policy == retry_policy


def test_stale_bound_document_context_requires_a_new_refreshed_turn() -> None:
    error = _stale_context()

    assert error.code == "DOCUMENT_CONTEXT_STALE"
    assert error.retry_policy == "refresh"


@pytest.mark.asyncio
async def test_contextual_locate_requires_read_and_allows_only_one_candidate(
    tmp_path: Path,
) -> None:
    service, store, _source, _ref, created, ctx = await _sent_contextual_button_context(
        tmp_path
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    first = '<button id="one">'
    second = '<button id="two">'
    try:
        unread = await _call(
            handler,
            "document_locate",
            {
                "annotation_order": 0,
                "operation": "remove_node",
                "candidateSource": second,
            },
        )
        assert unread.is_error is True
        assert "DOCUMENT_CANDIDATE_UNREAD" in unread.content

        read = await _call(handler, "document_read", {"view": "source"})
        assert read.is_error is False, read.content
        located = await _call(
            handler,
            "document_locate",
            {
                "annotation_order": 0,
                "operation": "remove_node",
                "candidateSource": second,
            },
        )
        assert located.is_error is False, located.content
        located_payload = json.loads(located.content)
        locations = located_payload["locations"]
        assert len(locations) == 1
        assert located_payload["selectionStatus"] == "contextual"
        assert located_payload["retryAllowed"] is False
        assert located_payload["nextAction"] == "apply_returned_grant"

        changed_candidate = await _call(
            handler,
            "document_locate",
            {
                "annotation_order": 0,
                "operation": "set_style",
                "candidateSource": first,
            },
        )
        assert changed_candidate.is_error is True
        assert "ARTIFACT_RANGE_QUERY_LIMIT" in changed_candidate.content

        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-contextual-apply")
        applied = await _call(
            handler,
            "document_apply",
            {"mutations": [{"grant_token": locations[0]["grantToken"]}]},
            tool_use_id="call-contextual-apply",
        )
        assert applied.is_error is False, applied.content
        document = await service.get_document(created.document.document_id)
        revision = await service.get_revision(document.head_revision_id)
        _new_ref, path = store.resolve_for_download(
            revision.artifact_id,
            session_id=SESSION_ID,
        )
        assert path.read_bytes() == b'<main><button id="one">One</button></main>'
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_ready_inspection_explains_grant_reuse_and_unavailable_operations(
    tmp_path: Path,
) -> None:
    service, _store, _source, _ref, _created, _anchor, ctx = await _sent_img_context(
        tmp_path
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        inspected = await _call(handler, "document_inspect", {})
        assert inspected.is_error is False, inspected.content
        payload = json.loads(inspected.content)
        assert payload["protocol"] == {
            "inspectAgain": False,
            "readyTargets": "reuse_matching_initialLocations",
            "contextualTargets": "document_read_then_document_locate_once",
            "unsupportedOperations": "leave_unchanged_and_explain",
        }
        annotation = payload["annotations"][0]
        assert annotation["selection"]["status"] == "ready"
        assert annotation["grantPolicy"] == {
            "reuseInitialLocations": True,
            "candidateSource": "forbidden",
            "availableInitialOperations": ["remove_node", "set_style"],
            "unavailableInitialOperations": ["replace_text"],
            "unsupportedOperationAction": "leave_unchanged_and_explain",
        }

        replay = await _call(handler, "document_inspect", {})
        assert replay.is_error is False, replay.content
        repeated = await _call(handler, "document_inspect", {})
        assert repeated.is_error is True
        assert "ARTIFACT_RANGE_QUERY_LIMIT" in repeated.content
        assert "Reuse the earlier result" in repeated.content
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_ready_locate_rejects_candidate_and_stops_deterministic_retries(
    tmp_path: Path,
) -> None:
    service, _store, _source, _ref, _created, _anchor, ctx = await _sent_img_context(
        tmp_path
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        candidate = '<img id="hero" src="photo.png">'
        unexpected = await _call(
            handler,
            "document_locate",
            {
                "annotation_order": 0,
                "operation": "replace_text",
                "candidateSource": candidate,
            },
        )
        assert unexpected.is_error is True
        assert "DOCUMENT_CANDIDATE_UNEXPECTED" in unexpected.content
        assert "Omit candidateSource for a ready target" in unexpected.content
        assert "initialLocations" in unexpected.content

        unsupported = await _call(
            handler,
            "document_locate",
            {"annotation_order": 0, "operation": "replace_text"},
        )
        assert unsupported.is_error is False, unsupported.content
        unsupported_payload = json.loads(unsupported.content)
        assert unsupported_payload["status"] == "not_found"
        assert unsupported_payload["selectionStatus"] == "ready"
        assert unsupported_payload["reasonCode"] == "DOCUMENT_OPERATION_UNAVAILABLE"
        assert unsupported_payload["retryAllowed"] is False
        assert (
            unsupported_payload["nextAction"]
            == "leave_unchanged_and_explain_without_retry"
        )

        repeated = await _call(
            handler,
            "document_locate",
            {"annotation_order": 0, "operation": "replace_text"},
        )
        assert repeated.is_error is True
        assert "ARTIFACT_RANGE_QUERY_LIMIT" in repeated.content
        assert "finish without calling it again" in repeated.content
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_contextual_candidate_may_span_adjacent_document_read_pages(
    tmp_path: Path,
) -> None:
    candidate = '<button id="boundary" aria-label="Across pages">'
    prefix = '<main><button id="one">One</button>'
    source_text = (
        prefix
        + ("x" * (250 - len(prefix)))
        + candidate
        + "Two</button></main>"
    )
    assert source_text.index(candidate) == 250
    service, _store, _source, _ref, _created, ctx = (
        await _sent_contextual_button_context(
            tmp_path,
            source_text=source_text,
        )
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        first_page = await _call(
            handler,
            "document_read",
            {"view": "source", "max_chars": 256},
        )
        assert first_page.is_error is False, first_page.content
        first_payload = json.loads(first_page.content)
        assert first_payload["nextCursor"]

        partial = await _call(
            handler,
            "document_locate",
            {
                "annotation_order": 0,
                "operation": "remove_node",
                "candidateSource": candidate,
            },
        )
        assert partial.is_error is True
        assert "DOCUMENT_CANDIDATE_UNREAD" in partial.content

        second_page = await _call(
            handler,
            "document_read",
            {
                "view": "source",
                "cursor": first_payload["nextCursor"],
                "max_chars": 256,
            },
        )
        assert second_page.is_error is False, second_page.content

        located = await _call(
            handler,
            "document_locate",
            {
                "annotation_order": 0,
                "operation": "remove_node",
                "candidateSource": candidate,
            },
        )
        assert located.is_error is False, located.content
        assert len(json.loads(located.content)["locations"]) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_document_removes_void_img_with_one_atomic_change_set(tmp_path: Path) -> None:
    service, store, source, ref, created, anchor, ctx = await _sent_img_context(tmp_path)
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        inspected = await _call(handler, "document_inspect", {})
        assert inspected.is_error is False, inspected.content
        payload = json.loads(inspected.content)
        assert payload["revision"]["sha256"] == ref.sha256
        locations = payload["annotations"][0]["initialLocations"]
        remove_location = next(
            item for item in locations if item["operation"] == "remove_node"
        )
        style_location = next(
            item for item in locations if item["operation"] == "set_style"
        )
        assert remove_location["grantToken"].startswith("hrg_")
        assert remove_location["expectsInput"] is False
        assert remove_location["inputKind"] is None
        assert remove_location["inputSchema"] is None
        assert remove_location["applyTemplate"] == {
            "grant_token": remove_location["grantToken"]
        }
        assert style_location["inputKind"] == "css_declarations"
        assert style_location["inputSchema"] == {
            "type": "string",
            "minLength": 1,
            "format": "css-declaration-list",
            "description": (
                "A CSS declaration list without selectors, rule braces, or a style wrapper."
            ),
            "examples": ["color: #222; background-color: #fff;"],
        }
        assert style_location["applyTemplate"] == {
            "grant_token": style_location["grantToken"],
            "input": "",
        }
        projection = json.dumps(payload, sort_keys=True)
        assert "start_offset" not in projection
        assert "end_offset" not in projection
        assert "target_fingerprint" not in projection
        assert created.document.document_id not in projection
        assert created.revision.revision_id not in projection
        assert anchor.anchor_id not in projection
        assert str(tmp_path) not in projection

        assert await service.list_change_sets(created.document.document_id) == ()

        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-document_apply")
        applied = await _call(
            handler,
            "document_apply",
            {
                "mutations": [
                    {
                        "grant_token": remove_location["grantToken"],
                    }
                ],
            },
        )
        assert applied.is_error is False, applied.content
        assert applied.terminates_turn is False
        result = json.loads(applied.content)
        assert result["status"] == "applied"
        assert result["change_set"]["mutation_kind"] == "document_semantic"

        document = await service.get_document(created.document.document_id)
        revision = await service.get_revision(document.head_revision_id)
        _new_ref, path = store.resolve_for_download(
            revision.artifact_id,
            session_id=SESSION_ID,
        )
        assert path.read_bytes() == b"<main><p>Keep me</p></main>"
        assert source != path.read_bytes()
        change_sets = await service.list_change_sets(created.document.document_id)
        assert len(change_sets) == 1
        assert change_sets[0].status is ChangeSetStatus.APPLIED
        assert change_sets[0].operations[0]["op"] == "document_semantic_mutation"
        assert len(await service.list_revisions(created.document.document_id)) == 2
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=TURN_ID,
            tool_use_id="call-document_apply",
        )
        assert receipt.status is MutationAttemptStatus.APPLIED

        replay = await _call(
            handler,
            "document_apply",
            {"mutations": [{"grant_token": remove_location["grantToken"]}]},
            tool_use_id="call-document_apply",
        )
        assert replay.is_error is False
        assert json.loads(replay.content)["status"] == "replayed"
        assert replay.effect_outcome is not None
        assert replay.effect_outcome.effect_state == "committed"

        mismatched_replay = await _call(
            handler,
            "document_apply",
            {
                "mutations": [
                    {
                        "grant_token": remove_location["grantToken"],
                        "input": "different proposal",
                    }
                ]
            },
            tool_use_id="call-document_apply",
        )
        assert mismatched_replay.is_error is True
        assert mismatched_replay.effect_outcome is not None
        assert mismatched_replay.effect_outcome.outcome_code == (
            "DOCUMENT_MUTATION_REPLAY_CONFLICT"
        )
        assert mismatched_replay.effect_outcome.effect_state == "committed"
        assert mismatched_replay.effect_outcome.retry_policy == "never"
        mismatch_outcome = mismatched_replay.effect_outcome.safe_details[
            "documentMutationOutcome"
        ]
        assert mismatch_outcome["status"] == "applied"
        assert mismatch_outcome["attemptId"] == receipt.mutation_attempt_id
        assert len(await service.list_revisions(created.document.document_id)) == 2
        assert len(await service.list_change_sets(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_document_remove_grant_rejects_even_empty_input_before_commit(
    tmp_path: Path,
) -> None:
    service, _store, _source, _ref, created, _anchor, ctx = await _sent_img_context(
        tmp_path
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        inspected = await _call(handler, "document_inspect", {})
        locations = json.loads(inspected.content)["annotations"][0]["initialLocations"]
        removal = next(item for item in locations if item["operation"] == "remove_node")
        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-empty-remove-value")

        result = await _call(
            handler,
            "document_apply",
            {
                "mutations": [
                    {
                        "grant_token": removal["grantToken"],
                        "input": "",
                    }
                ],
            },
            tool_use_id="call-empty-remove-value",
        )

        assert result.is_error is True
        assert "DOCUMENT_MUTATION_INPUT_UNEXPECTED" in result.content
        assert "omit the input field entirely" in result.content
        assert result.effect_outcome is not None
        assert result.effect_outcome.effect_state == "none"
        assert result.effect_outcome.retry_policy == "same_turn"
        assert await service.list_change_sets(created.document.document_id) == ()
        assert len(await service.list_revisions(created.document.document_id)) == 1
        with pytest.raises(ArtifactNotFoundError):
            await service.reconcile_mutation_attempt(
                document_id=created.document.document_id,
                turn_id=TURN_ID,
                tool_use_id="call-empty-remove-value",
            )
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_changed_replay_cannot_downgrade_an_ambiguous_durable_fact(
    tmp_path: Path,
) -> None:
    service, _store, _source, _ref, created, _anchor, ctx = await _sent_img_context(
        tmp_path
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await service.reserve_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=TURN_ID,
            tool_use_id="call-ambiguous-replay",
            base_revision_id=created.revision.revision_id,
            proposal_sha256="a" * 64,
        )
        receipt = await service.mark_mutation_attempt_ambiguous(
            document_id=created.document.document_id,
            turn_id=TURN_ID,
            tool_use_id="call-ambiguous-replay",
            failure_code="synthetic_commit_outcome_unknown",
        )

        replay = await _call(
            handler,
            "document_apply",
            {"mutations": [{"grant_token": "hrg_" + "A" * 43}]},
            tool_use_id="call-ambiguous-replay",
        )

        assert replay.is_error is True
        assert replay.effect_outcome is not None
        assert replay.effect_outcome.effect_state == "unknown"
        assert replay.effect_outcome.retry_policy == "reconcile"
        outcome = replay.effect_outcome.safe_details["documentMutationOutcome"]
        assert outcome["status"] == "ambiguous"
        assert outcome["attemptId"] == receipt.mutation_attempt_id
        reconciled = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=TURN_ID,
            tool_use_id="call-ambiguous-replay",
        )
        assert reconciled == receipt
        assert await service.list_change_sets(created.document.document_id) == ()
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_document_replaces_heading_and_removes_image_atomically(
    tmp_path: Path,
) -> None:
    service, store, source, _ref, created, ctx = await _sent_heading_and_img_context(tmp_path)
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        inspected = await _call(handler, "document_inspect", {})
        assert inspected.is_error is False, inspected.content
        annotations = json.loads(inspected.content)["annotations"]
        heading_location = next(
            item
            for item in annotations[0]["initialLocations"]
            if item["operation"] == "replace_text"
        )
        image_location = next(
            item
            for item in annotations[1]["initialLocations"]
            if item["operation"] == "remove_node"
        )

        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-document_apply")
        applied = await _call(
            handler,
            "document_apply",
            {
                "mutations": [
                    {
                        "grant_token": heading_location["grantToken"],
                        "input": "Translated heading",
                    },
                    {
                        "grant_token": image_location["grantToken"],
                    },
                ],
            },
        )
        assert applied.is_error is False, applied.content

        document = await service.get_document(created.document.document_id)
        revision = await service.get_revision(document.head_revision_id)
        _new_ref, path = store.resolve_for_download(
            revision.artifact_id,
            session_id=SESSION_ID,
        )
        expected = (
            b'<style>.title{color:red}</style><main>'
            b'<h1 class="title">Translated heading</h1>'
            b"<p>Keep me</p></main>"
        )
        assert path.read_bytes() == expected
        assert source.startswith(b"<style>.title{color:red}</style>")
        assert len(await service.list_revisions(created.document.document_id)) == 2
        change_sets = await service.list_change_sets(created.document.document_id)
        assert len(change_sets) == 1
        assert change_sets[0].status is ChangeSetStatus.APPLIED
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_document_apply_may_mutate_one_of_multiple_selections(
    tmp_path: Path,
) -> None:
    service, store, source, _ref, created, ctx = await _sent_heading_and_img_context(tmp_path)
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        inspected = await _call(handler, "document_inspect", {})
        annotations = json.loads(inspected.content)["annotations"]
        heading_location = next(
            item
            for item in annotations[0]["initialLocations"]
            if item["operation"] == "replace_text"
        )

        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-partial-selection")
        applied = await _call(
            handler,
            "document_apply",
            {
                "mutations": [
                    {
                        "grant_token": heading_location["grantToken"],
                        "input": "Only the heading changed",
                    }
                ]
            },
            tool_use_id="call-partial-selection",
        )

        assert applied.is_error is False, applied.content
        document = await service.get_document(created.document.document_id)
        revision = await service.get_revision(document.head_revision_id)
        _new_ref, path = store.resolve_for_download(
            revision.artifact_id,
            session_id=SESSION_ID,
        )
        candidate = path.read_bytes()
        assert b"Only the heading changed" in candidate
        assert b'<img id="hero" src="photo.png">' in candidate
        assert candidate != source
        assert len(await service.list_revisions(created.document.document_id)) == 2
        assert len(await service.list_change_sets(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_commit_conflict_returns_refresh_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _store, _source, _ref, created, _anchor, ctx = await _sent_img_context(
        tmp_path
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        inspected = await _call(handler, "document_inspect", {})
        locations = json.loads(inspected.content)["annotations"][0]["initialLocations"]
        removal = next(item for item in locations if item["operation"] == "remove_node")

        async def reject_stale_writer(**_kwargs):
            raise ArtifactConflictError("synthetic stale head")

        monkeypatch.setattr(service, "acquire_writer_lease", reject_stale_writer)
        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-conflict")
        result = await _call(
            handler,
            "document_apply",
            {"mutations": [removal["applyTemplate"]]},
            tool_use_id="call-conflict",
        )

        assert result.is_error is True
        assert result.effect_outcome is not None
        assert result.effect_outcome.retry_policy == "refresh"
        outcome = result.effect_outcome.safe_details["documentMutationOutcome"]
        assert outcome["status"] == "conflict"
        assert outcome["refreshRequired"] is True
        receipt = await service.reconcile_mutation_attempt(
            document_id=created.document.document_id,
            turn_id=TURN_ID,
            tool_use_id="call-conflict",
        )
        assert receipt.status is MutationAttemptStatus.FAILED
        assert receipt.failure_code == "DOCUMENT_MUTATION_CONFLICT"
        assert await service.list_change_sets(created.document.document_id) == ()
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cross_scope_grant_closes_write_boundary_without_durable_attempt(
    tmp_path: Path,
) -> None:
    service, _store, _source, _ref, created, _anchor, ctx = await _sent_img_context(
        tmp_path
    )
    handler = build_tool_handler(get_default_registry(), ctx)
    try:
        controller = ctx.artifact_mutation_attempt_controller
        assert controller is not None
        await controller.observe_intent("call-cross-scope")
        result = await _call(
            handler,
            "document_apply",
            {"mutations": [{"grant_token": "hrg_" + "A" * 43}]},
            tool_use_id="call-cross-scope",
        )

        assert result.is_error is True
        assert result.effect_outcome is not None
        assert result.effect_outcome.effect_state == "none"
        assert result.effect_outcome.retry_policy == "never"
        assert result.effect_outcome.loop_action == "finalize_without_tools"
        outcome = result.effect_outcome.safe_details["documentMutationOutcome"]
        assert outcome["status"] == "not_attempted"
        assert "refreshRequired" not in outcome
        assert controller.proposal_rejection_count == 1
        assert await service.list_unresolved_mutation_attempts() == ()
        assert await service.list_change_sets(created.document.document_id) == ()
        assert len(await service.list_revisions(created.document.document_id)) == 1
    finally:
        await service.close()
