"""Resource inventory, copy-import, and immutable publish RPC contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import opensquilla.gateway.rpc_workbench_resources as resource_rpc
from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionService,
    DocumentImportMode,
    DocumentSourceType,
    MutationAttemptStatus,
)
from opensquilla.artifacts import ArtifactBundle, ArtifactBundleSourceFile, ArtifactStore
from opensquilla.engine.types import ArtifactEvent
from opensquilla.gateway.generated_artifact_adoption import GeneratedArtifactAdopter
from opensquilla.gateway.rpc import RpcContext, RpcUnavailableError, get_dispatcher
from opensquilla.gateway.scopes import METHOD_SCOPES, READ_SCOPE, WRITE_SCOPE
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:workbench-resources"


def test_workbench_resource_method_scopes_are_fail_closed() -> None:
    assert METHOD_SCOPES["workbench.resources.list"] == READ_SCOPE
    assert METHOD_SCOPES["workbench.resources.get"] == READ_SCOPE
    assert METHOD_SCOPES["workbench.previews.create"] == READ_SCOPE
    assert METHOD_SCOPES["workbench.resources.open"] == WRITE_SCOPE
    assert METHOD_SCOPES["artifacts.mutations.resolve"] == WRITE_SCOPE
    assert METHOD_SCOPES["documents.import"] == WRITE_SCOPE
    assert METHOD_SCOPES["documents.publish"] == WRITE_SCOPE


@pytest.mark.parametrize(
    ("agent_editable", "selection_context", "expected_edit"),
    ((False, True, False), (True, False, True)),
)
def test_document_resource_capability_axes_are_independent(
    monkeypatch: pytest.MonkeyPatch,
    agent_editable: bool,
    selection_context: bool,
    expected_edit: bool,
) -> None:
    monkeypatch.setattr(
        resource_rpc,
        "_format_profile",
        lambda *_args, **_kwargs: resource_rpc._FormatProfile(
            kind=ArtifactKind.HTML,
            adapter=None,
            preview=True,
            editable=False,
            agent_editable=agent_editable,
            selection_context=selection_context,
            publishable=True,
            reason_code=None,
        ),
    )
    payload = resource_rpc._document_payload(
        SimpleNamespace(
            document_id="doc-independent",
            name="independent.html",
            created_at=1,
            updated_at=1,
        ),
        SimpleNamespace(
            revision_id="rev-independent",
            artifact_id="artifact-independent",
            media_type="text/html",
            byte_size=1,
            artifact_sha256="a" * 64,
        ),
        binding=None,
        publication=None,
        trusted_capabilities=True,
    )

    assert payload["capabilities"] == {
        "preview": True,
        "download": True,
        "selectionContext": selection_context,
        "manualEdit": False,
        "agentEdit": agent_editable,
        "edit": expected_edit,
        "publish": True,
        "editReasonCode": None,
    }


@pytest.mark.parametrize(
    ("resource_type", "id_field"),
    (
        ("attachment", "attachmentId"),
        ("document", "documentId"),
        ("deliverable", "artifactId"),
        ("url", "urlId"),
    ),
)
def test_workbench_resource_refs_use_discriminated_ids_with_legacy_alias(
    resource_type: str,
    id_field: str,
) -> None:
    resource_id = f"{resource_type}-fixture"
    assert resource_rpc._resource_ref(
        {"resource": {"type": resource_type, id_field: resource_id}}
    ) == (resource_type, resource_id)
    assert resource_rpc._resource_ref(
        {"resource": {"type": resource_type, "id": resource_id}}
    ) == (resource_type, resource_id)
    assert resource_rpc._resource_ref_payload(resource_type, resource_id) == {
        "type": resource_type,
        id_field: resource_id,
        "id": resource_id,
    }

    with pytest.raises(ValueError, match="must match"):
        resource_rpc._resource_ref(
            {
                "resource": {
                    "type": resource_type,
                    id_field: resource_id,
                    "id": "different-fixture",
                }
            }
        )


@pytest.mark.asyncio
async def test_multifile_deliverable_is_preview_only_and_never_truncated_on_import(
    resource_env,
) -> None:
    env = resource_env
    ref = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b"<link rel='stylesheet' href='style.css'><h1>bundle</h1>",
                ),
                ArtifactBundleSourceFile(
                    path="style.css",
                    mime="text/css",
                    data=b"h1 { color: red; }",
                ),
            ),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="bundle.html",
        mime="text/html",
        source="workbench-resource-bundle-test",
    )
    listed = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["deliverable"]},
    )
    assert listed.error is None, listed.error
    deliverable = next(
        item for item in listed.payload["resources"] if item["resource"]["id"] == ref.id
    )
    assert deliverable["capabilities"]["preview"] is True
    assert deliverable["capabilities"]["edit"] is False
    assert deliverable["capabilities"]["manualEdit"] is False
    assert deliverable["capabilities"]["agentEdit"] is False
    assert deliverable["capabilities"]["selectionContext"] is False

    imported = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "deliverable", "id": ref.id},
            "mode": "copy",
            "expectedSha256": ref.sha256,
            "idempotencyKey": "must-not-truncate-bundle",
        },
    )
    assert imported.error is not None
    assert imported.error.code == "RESOURCE_UNSUPPORTED"
    assert imported.error.details == {"reasonCode": "html_bundle_edit_not_supported"}
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()

    opened = await _dispatch(
        env,
        "workbench.resources.open",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {"type": "deliverable", "artifactId": ref.id},
        },
    )
    assert opened.error is None, opened.error
    assert opened.payload["disposition"] == "readonly"
    assert opened.payload["resolution"] == {"status": "readonly"}
    assert opened.payload["materialized"] is False
    assert opened.payload["reasonCode"] == "html_bundle_edit_not_supported"
    assert opened.payload["resource"]["resource"]["id"] == ref.id
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()


@pytest.mark.asyncio
async def test_stale_partial_single_file_bundle_is_safely_revalidated_for_editing(
    resource_env,
) -> None:
    env = resource_env
    stale_remote_import = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=(
                        b"<style>@import url('https://fonts.googleapis.com/css2?family=Inter');"
                        b"</style><h1>Legacy remote font</h1>"
                    ),
                ),
            ),
            collection_status="partial",
            warning_codes=("missing_dependency",),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="legacy-remote-font.html",
        mime="text/html",
        source="workbench-resource-stale-bundle-test",
    )
    actual_missing_dependency = env.store.publish_bundle(
        ArtifactBundle(
            entrypoint="index.html",
            files=(
                ArtifactBundleSourceFile(
                    path="index.html",
                    mime="text/html",
                    data=b"<link rel='stylesheet' href='missing.css'><h1>Incomplete</h1>",
                ),
            ),
            collection_status="partial",
            warning_codes=("missing_dependency",),
        ),
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="actual-missing-dependency.html",
        mime="text/html",
        source="workbench-resource-stale-bundle-test",
    )

    listed = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["deliverable"]},
    )
    assert listed.error is None, listed.error
    capabilities_by_id = {
        item["resource"]["id"]: item["capabilities"]
        for item in listed.payload["resources"]
    }
    assert capabilities_by_id[stale_remote_import.id]["manualEdit"] is True
    assert capabilities_by_id[actual_missing_dependency.id]["manualEdit"] is False

    imported = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "deliverable", "id": stale_remote_import.id},
            "mode": "copy",
            "expectedSha256": stale_remote_import.sha256,
            "idempotencyKey": "import-revalidated-stale-single-file-bundle",
        },
    )
    assert imported.error is None, imported.error

    source = await _dispatch(
        env,
        "artifacts.source.read",
        {
            "sessionKey": SESSION_KEY,
            "documentId": imported.payload["document"]["id"],
        },
    )
    assert source.error is None, source.error

    unchanged_manifest = env.store.describe_preview_bundle(
        stale_remote_import.id,
        session_id=env.session.session_id,
    )
    assert unchanged_manifest is not None
    assert unchanged_manifest.collection_status == "partial"
    assert unchanged_manifest.warning_codes == ("missing_dependency",)

    rejected = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "deliverable", "id": actual_missing_dependency.id},
            "mode": "copy",
            "expectedSha256": actual_missing_dependency.sha256,
            "idempotencyKey": "reject-actual-missing-dependency-bundle",
        },
    )
    assert rejected.error is not None
    assert rejected.error.code == "RESOURCE_UNSUPPORTED"
    assert rejected.error.details == {"reasonCode": "html_bundle_edit_not_supported"}

@pytest.fixture
async def resource_env(tmp_path: Path):
    storage = SessionStorage(":memory:")
    await storage.connect()
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    session = await manager.create(SESSION_KEY)
    config = SimpleNamespace(
        attachments=SimpleNamespace(
            media_root=str(media_root),
            persist_transcripts=True,
        ),
        state_dir=str(tmp_path / "state"),
        config_path=None,
    )
    ctx = RpcContext(
        conn_id="workbench-resource-test",
        session_manager=manager,
        config=config,
    )
    try:
        yield SimpleNamespace(
            storage=storage,
            manager=manager,
            session=session,
            store=ArtifactStore(media_root),
            config=config,
            ctx=ctx,
        )
    finally:
        await storage.close()


async def _dispatch(env, method: str, params: dict[str, object]):
    return await get_dispatcher().dispatch(f"test:{method}", method, params, env.ctx)


@pytest.mark.asyncio
async def test_generated_deliverable_helper_adopts_supported_html_without_copying(
    resource_env,
) -> None:
    env = resource_env
    ref = env.store.publish_bytes(
        b"<!doctype html><h1>generated document</h1>",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="generated.html",
        mime="text/html",
        source="generated-helper-test",
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)

    first = await resource_rpc.adopt_generated_deliverable_if_editable(
        service=service,
        store=env.store,
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        ref=ref,
        actor=Actor(ActorKind.AGENT, "agent-main"),
    )
    second = await resource_rpc.adopt_generated_deliverable_if_editable(
        service=service,
        store=env.store,
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        ref=ref,
        actor=Actor(ActorKind.AGENT, "agent-main"),
    )

    assert first is not None
    assert second is not None
    document, revision, binding, created = first
    replay_document, replay_revision, replay_binding, replay_created = second
    assert created is True
    assert replay_created is False
    assert replay_document == document
    assert replay_revision == revision
    assert replay_binding == binding
    assert revision.artifact_id == ref.id
    assert binding.source_resource_id == ref.id

    opened = await _dispatch(
        env,
        "workbench.resources.open",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {"type": "deliverable", "artifactId": ref.id},
        },
    )
    assert opened.error is None, opened.error
    assert opened.payload["disposition"] == "document"
    assert opened.payload["resolution"] == {"status": "current"}
    assert opened.payload["materialized"] is False
    assert opened.payload["document"]["documentId"] == document.document_id
    assert opened.payload["revision"]["artifactId"] == ref.id
    assert opened.payload["resource"]["resource"]["type"] == "document"


@pytest.mark.asyncio
async def test_mutation_resolution_projects_only_product_outcomes(resource_env) -> None:
    env = resource_env
    original = b"<!doctype html><h1>Before</h1>"
    ref = env.store.publish_bytes(
        original,
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="resolve.html",
        mime="text/html",
        source="mutation-resolution-test",
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    adopted = await resource_rpc.adopt_generated_deliverable_if_editable(
        service=service,
        store=env.store,
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        ref=ref,
        actor=Actor(ActorKind.AGENT, "agent-main"),
    )
    assert adopted is not None
    document, revision, _binding, _created = adopted

    missing = await _dispatch(
        env,
        "artifacts.mutations.resolve",
        {
            "sessionKey": SESSION_KEY,
            "operation": "source.patch",
            "requestId": "missing-request",
            "documentId": document.document_id,
        },
    )
    assert missing.error is None
    # The resolve request can race durable admission of an already-sent write.
    # Absence is therefore unknown, never proof that a new request ID is safe.
    assert missing.payload == {"status": "pending", "retryAfterMs": 250}

    for operation in (
        "document.import",
        "workbench.resources.open",
        "document.publish",
    ):
        admission_race = await _dispatch(
            env,
            "artifacts.mutations.resolve",
            {
                "sessionKey": SESSION_KEY,
                "operation": operation,
                "requestId": f"missing-{operation}",
            },
        )
        assert admission_race.error is None
        assert admission_race.payload == {"status": "pending", "retryAfterMs": 250}

    pending_request_id = "pending-request"
    pending_turn_id = f"manual-source-patch:{pending_request_id}"
    pending_tool_id = "rpc-source-patch:pending-test"
    await service.reserve_mutation_attempt(
        document_id=document.document_id,
        turn_id=pending_turn_id,
        tool_use_id=pending_tool_id,
        base_revision_id=revision.revision_id,
        proposal_sha256="b" * 64,
    )
    pending = await _dispatch(
        env,
        "artifacts.mutations.resolve",
        {
            "sessionKey": SESSION_KEY,
            "operation": "source.patch",
            "clientRequestId": pending_request_id,
            "documentId": document.document_id,
        },
    )
    assert pending.error is None
    assert pending.payload == {"status": "pending", "retryAfterMs": 250}

    await service.mark_mutation_attempt_failed(
        document_id=document.document_id,
        turn_id=pending_turn_id,
        tool_use_id=pending_tool_id,
        failure_code="synthetic_failure",
    )
    failed = await _dispatch(
        env,
        "artifacts.mutations.resolve",
        {
            "sessionKey": SESSION_KEY,
            "operation": "source.patch",
            "requestId": pending_request_id,
            "documentId": document.document_id,
        },
    )
    assert failed.error is None
    assert failed.payload == {"status": "not_applied"}

    applied_request_id = "applied-request"
    applied = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document.document_id,
            "expectedHeadRevisionId": revision.revision_id,
            "expectedStateRevision": document.state_revision,
            "expectedSourceSha256": revision.artifact_sha256,
            "offsetEncoding": "unicode-code-point",
            "patches": [
                {
                    "startOffset": 0,
                    "endOffset": len(original.decode("utf-8")),
                    "replacement": "<!doctype html><h1>After</h1>",
                }
            ],
            "clientRequestId": applied_request_id,
        },
    )
    assert applied.error is None, applied.error
    resolved = await _dispatch(
        env,
        "artifacts.mutations.resolve",
        {
            "sessionKey": SESSION_KEY,
            "operation": "source.patch",
            "requestId": applied_request_id,
            "documentId": document.document_id,
        },
    )
    assert resolved.error is None, resolved.error
    assert resolved.payload["status"] == "applied"
    assert resolved.payload["result"] == {
        "documentId": document.document_id,
        "revisionId": applied.payload["source"]["revisionId"],
        "sha256": applied.payload["source"]["sha256"],
        "stateRevision": applied.payload["source"]["stateRevision"],
    }
    serialized = json.dumps(resolved.payload, sort_keys=True)
    for internal_name in (
        "attemptId",
        "baseRevisionId",
        "changeSetId",
        "editSessionId",
        "leaseId",
        "receipt",
    ):
        assert internal_name not in serialized


@pytest.mark.asyncio
async def test_mutation_resolution_rejects_unknown_operation_with_safe_error(
    resource_env,
) -> None:
    response = await _dispatch(
        resource_env,
        "artifacts.mutations.resolve",
        {
            "sessionKey": SESSION_KEY,
            "operation": "private.receipt.inspect",
            "requestId": "safe-error-request",
        },
    )
    assert response.error is not None
    assert response.error.code == "INVALID_REQUEST"
    assert "private.receipt.inspect" not in response.error.message
    assert response.error.details["correlationId"]


@pytest.mark.asyncio
async def test_generated_artifact_adopter_emits_once_after_atomic_adoption(
    resource_env,
) -> None:
    env = resource_env
    ref = env.store.publish_bytes(
        b"<!doctype html><h1>generated through turn stream</h1>",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="stream-generated.html",
        mime="text/html",
        source="generated-adopter-test",
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    emitted: list[dict[str, object]] = []

    async def emit(payload: dict[str, object]) -> None:
        emitted.append(payload)

    adopter = GeneratedArtifactAdopter(
        service=service,
        store=env.store,
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        event_emitter=emit,
    )
    event = ArtifactEvent(**ref.to_dict())

    await adopter(event)
    await adopter(event)

    documents = await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    )
    assert len(documents) == 1
    revisions = await service.list_revisions(documents[0].document_id, limit=10)
    assert len(revisions) == 1
    assert revisions[0].artifact_id == ref.id
    assert emitted == [
        {
            "artifactEventSeq": 1,
            "documentId": documents[0].document_id,
            "revisionId": revisions[0].revision_id,
            "changeSetId": None,
            "action": "document.created",
        }
    ]


@pytest.mark.asyncio
async def test_resource_reads_do_not_change_sqlite_rows(resource_env) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="read-only-resource",
        name="read-only.html",
        payload=b"<h1>read only</h1>",
        staged=False,
    )
    # Warm the additive schema seam once, exactly as Gateway boot does.
    await ArtifactSessionService.from_session_storage(env.storage)
    changes_before = env.storage.conn.total_changes

    listed = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY},
    )
    fetched = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "attachmentId": attachment["attachment_id"],
            },
        },
    )
    legacy_fetched = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": attachment["attachment_id"]},
        },
    )
    previewed = await _dispatch(
        env,
        "workbench.previews.create",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "attachmentId": attachment["attachment_id"],
            },
            "mode": "isolated",
        },
    )

    assert listed.error is None, listed.error
    assert fetched.error is None, fetched.error
    assert fetched.payload["resource"]["resource"] == {
        "type": "attachment",
        "attachmentId": attachment["attachment_id"],
        "id": attachment["attachment_id"],
    }
    assert legacy_fetched.error is None, legacy_fetched.error
    assert legacy_fetched.payload == fetched.payload
    assert previewed.error is None, previewed.error
    assert env.storage.conn.total_changes == changes_before


@pytest.mark.asyncio
async def test_resource_list_does_not_reserve_sqlite_writer_slot(tmp_path: Path) -> None:
    db_path = tmp_path / "resource-reads.sqlite3"
    storage = SessionStorage(str(db_path))
    await storage.connect()
    media_root = tmp_path / "media"
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    session = await manager.create(SESSION_KEY)
    config = SimpleNamespace(
        attachments=SimpleNamespace(
            media_root=str(media_root),
            persist_transcripts=True,
        ),
        state_dir=str(tmp_path / "state"),
        config_path=None,
    )
    env = SimpleNamespace(
        storage=storage,
        manager=manager,
        session=session,
        store=ArtifactStore(media_root),
        config=config,
        ctx=RpcContext(
            conn_id="workbench-resource-read-lock-test",
            session_manager=manager,
            config=config,
        ),
    )
    blocker = sqlite3.connect(db_path, isolation_level=None)
    try:
        # Boot-time reconciliation is complete before the competing writer starts.
        await ArtifactSessionService.from_session_storage(storage)
        blocker.execute("PRAGMA journal_mode=WAL")
        blocker.execute("BEGIN IMMEDIATE")

        listed = await _dispatch(
            env,
            "workbench.resources.list",
            {"sessionKey": SESSION_KEY},
        )

        assert listed.error is None, listed.error
        assert listed.payload["resources"] == []
    finally:
        if blocker.in_transaction:
            blocker.rollback()
        blocker.close()
        await storage.close()


async def _append_attachment(
    env,
    *,
    message_id: str,
    name: str,
    payload: bytes,
    staged: bool,
    mime: str = "text/html",
) -> dict[str, object]:
    attachment: dict[str, object] = {
        "type": mime,
        "data": base64.b64encode(payload).decode("ascii"),
        "name": name,
    }
    if staged:
        attachment["_was_staged"] = True
    envelope, _writes = build_transcript_attachment_envelope(
        text=f"uploaded {name}",
        attachments=[attachment],
        session_id=env.session.session_id,
        media_root=Path(env.config.attachments.media_root),
        persist_enabled=True,
    )
    await env.storage.append_transcript_entry(
        TranscriptEntry(
            session_id=env.session.session_id,
            session_key=SESSION_KEY,
            message_id=message_id,
            role="user",
            content=envelope,
        )
    )
    return json.loads(envelope)["attachments"][0]


async def _import_attachment(env, attachment_id: str, *, key: str):
    resolved = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": attachment_id},
        },
    )
    assert resolved.error is None, resolved.error
    response = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "attachment", "attachmentId": attachment_id},
            "mode": "copy",
            "expectedSha256": resolved.payload["resource"]["sha256"],
            "idempotencyKey": key,
        },
    )
    assert response.error is None, response.error
    return response.payload


@pytest.mark.asyncio
async def test_resources_open_silently_materializes_legacy_html_sources(resource_env) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="silent-open-attachment",
        name="uploaded.html",
        payload=b"<!doctype html><h1>uploaded</h1>",
        staged=True,
    )
    deliverable = env.store.publish_bytes(
        b"<!doctype html><h1>published</h1>",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="published.html",
        mime="text/html",
        source="silent-open-test",
    )
    source_refs = (
        (
            {
                "type": "attachment",
                "attachmentId": str(attachment["attachment_id"]),
            },
            hashlib.sha256(b"<!doctype html><h1>uploaded</h1>").hexdigest(),
        ),
        (
            {"type": "deliverable", "artifactId": deliverable.id},
            deliverable.sha256,
        ),
    )

    document_ids: list[str] = []
    for index, (source_ref, source_sha256) in enumerate(source_refs):
        first = await _dispatch(
            env,
            "workbench.resources.open",
            {
                "sessionKey": SESSION_KEY,
                "resourceRef": source_ref,
                "intent": "edit-current",
                "expectedSha256": source_sha256,
                "idempotencyKey": f"silent-open-{index}",
            },
        )
        assert first.error is None, first.error
        assert first.payload["disposition"] == "document"
        assert first.payload["resolution"] == {"status": "materialized"}
        assert first.payload["materialized"] is True
        assert first.payload["resource"]["resource"]["type"] == "document"
        assert first.payload["binding"]["source"]["type"] == source_ref["type"]
        document_id = first.payload["document"]["documentId"]
        document_ids.append(document_id)
        assert first.payload["resource"]["resource"]["documentId"] == document_id
        assert first.payload["revision"]["revisionId"] == (
            first.payload["document"]["headRevisionId"]
        )

        replay = await _dispatch(
            env,
            "workbench.resources.open",
            {"sessionKey": SESSION_KEY, "resource": source_ref},
        )
        assert replay.error is None, replay.error
        assert replay.payload["disposition"] == "document"
        assert replay.payload["resolution"] == {"status": "current"}
        assert replay.payload["materialized"] is False
        assert replay.payload["document"]["documentId"] == document_id
        assert replay.payload["revision"]["revisionId"] == (
            first.payload["revision"]["revisionId"]
        )

    assert len(set(document_ids)) == 2
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert len(
        await service.list_documents(
            session_key=SESSION_KEY,
            session_id=env.session.session_id,
            limit=10,
        )
    ) == 2


@pytest.mark.asyncio
async def test_attachment_preview_descriptor_is_read_only_and_content_free(resource_env) -> None:
    env = resource_env
    source = b"<!doctype html><h1>private preview heading</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-preview-only",
        name="preview.html",
        payload=source,
        staged=True,
    )

    response = await _dispatch(
        env,
        "workbench.previews.create",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "id": attachment["attachment_id"],
            },
            "mode": "isolated",
        },
    )

    assert response.error is None, response.error
    preview = response.payload["preview"]
    assert preview["sandboxProfile"] == "opaque-offline"
    assert preview["network"] is False
    assert preview["adapter"]["sourceSha256"] == hashlib.sha256(source).hexdigest()
    assert "private preview heading" not in repr(response.payload)
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()


@pytest.mark.asyncio
async def test_invalid_html_attachments_fail_closed_without_read_side_writes(resource_env) -> None:
    env = resource_env
    invalid_encoding = await _append_attachment(
        env,
        message_id="message-invalid-encoding",
        name="invalid-encoding.html",
        payload=b"<h1>\xff</h1>",
        staged=True,
    )
    invalid_structure = await _append_attachment(
        env,
        message_id="message-invalid-structure",
        name="invalid-structure.html",
        payload=b"",
        staged=True,
    )
    changes_before_reads = env.storage.conn.total_changes

    expected_reasons = {
        str(invalid_encoding["attachment_id"]): "html_encoding_unsupported",
        str(invalid_structure["attachment_id"]): "html_validation_failed",
    }
    for attachment_id, expected_reason in expected_reasons.items():
        resolved = await _dispatch(
            env,
            "workbench.resources.get",
            {
                "sessionKey": SESSION_KEY,
                "resource": {"type": "attachment", "id": attachment_id},
            },
        )
        assert resolved.error is None, resolved.error
        capabilities = resolved.payload["resource"]["capabilities"]
        assert capabilities["preview"] is False
        assert capabilities["edit"] is False
        assert capabilities["manualEdit"] is False
        assert capabilities["agentEdit"] is False
        assert capabilities["selectionContext"] is False
        assert capabilities["editReasonCode"] == expected_reason

        preview = await _dispatch(
            env,
            "workbench.previews.create",
            {
                "sessionKey": SESSION_KEY,
                "resourceRef": {"type": "attachment", "id": attachment_id},
            },
        )
        assert preview.error is not None
        assert preview.error.code == "RESOURCE_UNSUPPORTED"
        assert preview.error.details == {"reasonCode": expected_reason}

        opened = await _dispatch(
            env,
            "workbench.resources.open",
            {
                "sessionKey": SESSION_KEY,
                "resourceRef": {"type": "attachment", "id": attachment_id},
            },
        )
        assert opened.error is None, opened.error
        assert opened.payload["disposition"] == "readonly"
        assert opened.payload["reasonCode"] == expected_reason
        assert opened.payload["resource"]["resource"]["id"] == attachment_id

    assert env.storage.conn.total_changes == changes_before_reads
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()


@pytest.mark.asyncio
async def test_oversized_html_capabilities_fail_before_doomed_ui_actions(resource_env) -> None:
    env = resource_env
    edit_too_large = await _append_attachment(
        env,
        message_id="message-edit-too-large",
        name="edit-too-large.html",
        payload=b"<main>" + (b"x" * (2 * 1024 * 1024)) + b"</main>",
        staged=True,
    )
    preview_too_large = await _append_attachment(
        env,
        message_id="message-preview-too-large",
        name="preview-too-large.html",
        payload=b"<main>" + (b"x" * (5 * 1024 * 1024)) + b"</main>",
        staged=True,
    )

    editable_capabilities = (
        await _dispatch(
            env,
            "workbench.resources.get",
            {
                "sessionKey": SESSION_KEY,
                "resource": {
                    "type": "attachment",
                    "id": edit_too_large["attachment_id"],
                },
            },
        )
    ).payload["resource"]["capabilities"]
    assert editable_capabilities == {
        "preview": True,
        "download": True,
        "selectionContext": False,
        "manualEdit": False,
        "agentEdit": False,
        "edit": False,
        "publish": False,
        "previewReasonCode": None,
        "editReasonCode": "html_edit_size_unsupported",
    }

    preview_capabilities = (
        await _dispatch(
            env,
            "workbench.resources.get",
            {
                "sessionKey": SESSION_KEY,
                "resource": {
                    "type": "attachment",
                    "id": preview_too_large["attachment_id"],
                },
            },
        )
    ).payload["resource"]["capabilities"]
    assert preview_capabilities == {
        "preview": False,
        "download": True,
        "selectionContext": False,
        "manualEdit": False,
        "agentEdit": False,
        "edit": False,
        "publish": False,
        "previewReasonCode": "html_preview_size_unsupported",
        "editReasonCode": "html_edit_size_unsupported",
    }
    preview = await _dispatch(
        env,
        "workbench.previews.create",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "attachment",
                "id": preview_too_large["attachment_id"],
            },
        },
    )
    assert preview.error is not None
    assert preview.error.code == "RESOURCE_UNSUPPORTED"
    assert preview.error.details == {"reasonCode": "html_preview_size_unsupported"}

    for attachment_id in (
        str(edit_too_large["attachment_id"]),
        str(preview_too_large["attachment_id"]),
    ):
        opened = await _dispatch(
            env,
            "workbench.resources.open",
            {
                "sessionKey": SESSION_KEY,
                "resourceRef": {"type": "attachment", "id": attachment_id},
            },
        )
        assert opened.error is None, opened.error
        assert opened.payload["disposition"] == "readonly"
        assert opened.payload["reasonCode"] == "html_edit_size_unsupported"


@pytest.mark.asyncio
async def test_resource_inventory_preserves_inline_and_staged_attachment_occurrences(
    resource_env,
) -> None:
    env = resource_env
    html = b"<!doctype html><h1>same bytes</h1>"
    first = await _append_attachment(
        env,
        message_id="message-staged-one",
        name="first.html",
        payload=html,
        staged=True,
    )
    second = await _append_attachment(
        env,
        message_id="message-staged-two",
        name="second.html",
        payload=html,
        staged=True,
    )
    inline = await _append_attachment(
        env,
        message_id="message-inline",
        name="inline.html",
        payload=html,
        staged=False,
    )

    ids = {str(first["attachment_id"]), str(second["attachment_id"]), str(inline["attachment_id"])}
    assert len(ids) == 3
    sha = hashlib.sha256(html).hexdigest()
    transcript_dir = (
        Path(env.config.attachments.media_root) / "transcripts" / env.session.session_id
    )
    assert list(transcript_dir.iterdir()) == [transcript_dir / sha]

    changes_before_read = env.storage.conn.total_changes
    listed = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY},
    )
    assert listed.error is None, listed.error
    attachments = [
        item for item in listed.payload["resources"] if item["resource"]["type"] == "attachment"
    ]
    assert len(attachments) == 3
    assert {item["resource"]["id"] for item in attachments} == ids
    assert {item["sha256"] for item in attachments} == {sha}
    assert {item["relations"]["messageId"] for item in attachments} == {
        "message-staged-one",
        "message-staged-two",
        "message-inline",
    }
    assert all(item["capabilities"]["edit"] is True for item in attachments)
    assert all(item["capabilities"]["manualEdit"] is True for item in attachments)
    assert all(item["capabilities"]["agentEdit"] is False for item in attachments)
    assert all(item["capabilities"]["selectionContext"] is False for item in attachments)
    assert all(item["capabilities"]["publish"] is False for item in attachments)
    assert all(
        "downloadUrl" in item for item in attachments if item["name"] != "inline.html"
    )
    assert "downloadUrl" not in next(
        item for item in attachments if item["name"] == "inline.html"
    )

    inline_get = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": inline["attachment_id"]},
        },
    )
    assert inline_get.error is None, inline_get.error
    inline_url = inline_get.payload["resource"]["downloadUrl"]
    assert inline_url.startswith("data:text/html;base64,")
    assert base64.b64decode(inline_url.split(",", 1)[1], validate=True) == html
    assert env.storage.conn.total_changes == changes_before_read

    imported_first = await _import_attachment(
        env,
        str(first["attachment_id"]),
        key="import-occurrence-one",
    )
    imported_second = await _import_attachment(
        env,
        str(second["attachment_id"]),
        key="import-occurrence-two",
    )
    assert imported_first["document"]["id"] != imported_second["document"]["id"]
    assert imported_first["binding"]["source"]["id"] == first["attachment_id"]
    assert imported_first["binding"]["source"]["attachmentId"] == first["attachment_id"]
    assert imported_second["binding"]["source"]["id"] == second["attachment_id"]
    assert imported_second["binding"]["source"]["attachmentId"] == second["attachment_id"]
    assert imported_first["binding"]["bindingId"] == imported_first["binding"]["id"]

    replayed = await _import_attachment(
        env,
        str(first["attachment_id"]),
        key="import-occurrence-one",
    )
    assert replayed["document"]["id"] == imported_first["document"]["id"]
    assert replayed["receipt"]["replayed"] is True

    after = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["document"]},
    )
    assert after.error is None, after.error
    assert len(after.payload["resources"]) == 2
    for document in after.payload["resources"]:
        capabilities = document["capabilities"]
        assert capabilities["preview"] is True
        assert capabilities["manualEdit"] is True
        assert capabilities["agentEdit"] is True
        assert capabilities["selectionContext"] is True
        assert capabilities["publish"] is True
        assert document["relations"]["headArtifactId"]
        assert document["relations"]["headRevisionId"]
        source_ref = document["relations"]["source"]
        assert source_ref["type"] == "attachment"
        assert source_ref["attachmentId"] == source_ref["id"]
        assert "revisionId=" in document["downloadUrl"]


@pytest.mark.asyncio
async def test_historical_attachment_ids_are_stable_per_message_occurrence(
    resource_env,
) -> None:
    env = resource_env
    html = b"<h1>historical</h1>"
    for message_id, name in (("legacy-one", "one.html"), ("legacy-two", "two.html")):
        attachment = {
            "type": "text/html",
            "data": base64.b64encode(html).decode("ascii"),
            "name": name,
            "_was_staged": True,
        }
        envelope, _writes = build_transcript_attachment_envelope(
            text="historical upload",
            attachments=[attachment],
            session_id=env.session.session_id,
            media_root=Path(env.config.attachments.media_root),
            persist_enabled=True,
        )
        raw = json.loads(envelope)
        raw["attachments"][0].pop("attachment_id")
        await env.storage.append_transcript_entry(
            TranscriptEntry(
                session_id=env.session.session_id,
                session_key=SESSION_KEY,
                message_id=message_id,
                role="user",
                content=json.dumps(raw),
            )
        )

    params = {"sessionKey": SESSION_KEY, "types": ["attachment"]}
    first = await _dispatch(env, "workbench.resources.list", params)
    second = await _dispatch(env, "workbench.resources.list", params)
    assert first.error is None, first.error
    assert second.error is None, second.error
    first_ids = [item["resource"]["id"] for item in first.payload["resources"]]
    second_ids = [item["resource"]["id"] for item in second.payload["resources"]]
    assert first_ids == second_ids
    assert len(set(first_ids)) == 2
    assert all(item.startswith("att_legacy_") for item in first_ids)


@pytest.mark.asyncio
async def test_import_is_session_scoped_and_recovers_reserved_candidate_after_crash(
    resource_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="message-crash",
        name="crash.html",
        payload=b"<h1>recover</h1>",
        staged=True,
    )
    attachment_id = str(attachment["attachment_id"])
    original = resource_rpc._ensure_internal_candidate
    failed_once = False

    async def _write_then_crash(*args, **kwargs):
        nonlocal failed_once
        result = await original(*args, **kwargs)
        if not failed_once:
            failed_once = True
            raise RpcUnavailableError("synthetic crash after candidate write")
        return result

    monkeypatch.setattr(resource_rpc, "_ensure_internal_candidate", _write_then_crash)
    params = {
        "sessionKey": SESSION_KEY,
        "source": {"type": "attachment", "id": attachment_id},
        "mode": "copy",
        "expectedSha256": attachment["sha256_ref"],
        "idempotencyKey": "import-after-crash",
    }
    interrupted = await _dispatch(env, "documents.import", params)
    assert interrupted.error is not None
    assert interrupted.error.code == "MUTATION_OUTCOME_PENDING"
    assert interrupted.error.accepted is None
    assert interrupted.error.details is not None
    assert interrupted.error.details["correlationId"]
    visible_error = f"{interrupted.error.message} {interrupted.error.details}".lower()
    assert all(term not in visible_error for term in ("candidate", "journal", "receipt"))

    service = await ArtifactSessionService.from_session_storage(env.storage)
    attempt = await service.get_document_import_attempt(
        session_id=env.session.session_id,
        idempotency_key="import-after-crash",
    )
    assert attempt.status is MutationAttemptStatus.RESERVED
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()
    assert env.store.list_refs(session_id=env.session.session_id, limit=10).refs == ()

    monkeypatch.setattr(resource_rpc, "_ensure_internal_candidate", original)
    recovered = await _dispatch(env, "documents.import", params)
    assert recovered.error is None, recovered.error
    assert recovered.payload["receipt"]["status"] == "applied"
    assert recovered.payload["receipt"]["replayed"] is True
    documents = await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    )
    assert len(documents) == 1

    other_key = "agent:main:webchat:workbench-resources-other"
    await env.manager.create(other_key)
    cross_session = await _dispatch(
        env,
        "documents.import",
        {
            **params,
            "sessionKey": other_key,
            "idempotencyKey": "cross-session-import",
        },
    )
    assert cross_session.error is not None
    assert cross_session.error.code == "DOCUMENT_UNAVAILABLE"
    assert cross_session.error.details == {"reasonCode": "resource_unavailable"}


@pytest.mark.asyncio
async def test_import_expected_hash_is_validated_and_bound_to_idempotency(
    resource_env,
) -> None:
    env = resource_env
    payload = b"<h1>expected hash</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-expected-hash",
        name="expected.html",
        payload=payload,
        staged=True,
    )
    expected = hashlib.sha256(payload).hexdigest()
    service = await ArtifactSessionService.from_session_storage(env.storage)
    changes_before_invalid = env.storage.conn.total_changes
    base_params = {
        "sessionKey": SESSION_KEY,
        "source": {"type": "attachment", "id": attachment["attachment_id"]},
        "mode": "copy",
        "clientRequestId": "expected-hash-import",
    }
    for rejected_params in (
        base_params,
        {**base_params, "expectedSha256": "not-a-sha256"},
        {
            **base_params,
            "expectedSha256": expected,
            "idempotencyKey": "different-request-id",
        },
    ):
        rejected = await _dispatch(env, "documents.import", rejected_params)
        assert rejected.error is not None
        assert rejected.error.code == "INVALID_REQUEST"
    assert env.storage.conn.total_changes == changes_before_invalid
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()

    params = {
        **base_params,
        "expectedSha256": expected,
    }
    imported = await _dispatch(env, "documents.import", params)
    assert imported.error is None, imported.error
    assert imported.payload["document"]["documentId"] == imported.payload["document"]["id"]
    assert imported.payload["revision"]["revisionId"] == imported.payload["revision"]["id"]
    assert imported.payload["document"]["head"]["revisionId"] == imported.payload[
        "revision"
    ]["revisionId"]
    assert imported.payload["binding"]["sourceSha256"] == expected
    assert imported.payload["receipt"]["requestId"] == "expected-hash-import"
    assert imported.payload["receipt"]["idempotencyKey"] == "expected-hash-import"

    replay = await _dispatch(
        env,
        "documents.import",
        {**params, "idempotencyKey": "expected-hash-import"},
    )
    assert replay.error is None, replay.error
    assert replay.payload["receipt"]["replayed"] is True
    assert replay.payload["document"]["id"] == imported.payload["document"]["id"]

    mismatch = await _dispatch(
        env,
        "documents.import",
        {**params, "expectedSha256": "0" * 64},
    )
    assert mismatch.error is not None
    assert mismatch.error.code == "DOCUMENT_CHANGED"
    assert "hash" not in mismatch.error.message.lower()
    assert mismatch.error.details["correlationId"]


@pytest.mark.asyncio
async def test_import_concurrent_same_request_creates_one_initial_revision(resource_env) -> None:
    env = resource_env
    payload = b"<h1>double click</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-concurrent-import",
        name="double-click.html",
        payload=payload,
        staged=True,
    )
    params = {
        "sessionKey": SESSION_KEY,
        "source": {
            "type": "attachment",
            "attachmentId": attachment["attachment_id"],
        },
        "mode": "copy",
        "expectedSha256": hashlib.sha256(payload).hexdigest(),
        "clientRequestId": "concurrent-double-click",
    }

    first, second = await asyncio.gather(
        _dispatch(env, "documents.import", params),
        _dispatch(env, "documents.import", params),
    )

    assert first.error is None, first.error
    assert second.error is None, second.error
    responses = (first.payload, second.payload)
    assert {response["document"]["documentId"] for response in responses} == {
        first.payload["document"]["documentId"]
    }
    assert {response["revision"]["revisionId"] for response in responses} == {
        first.payload["revision"]["revisionId"]
    }
    assert sorted(response["receipt"]["replayed"] for response in responses) == [False, True]

    service = await ArtifactSessionService.from_session_storage(env.storage)
    documents = await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    )
    assert len(documents) == 1
    revisions = await service.list_revisions(documents[0].document_id, limit=10)
    assert len(revisions) == 1
    assert revisions[0].generation == 1
    assert revisions[0].source.value == "initial"


@pytest.mark.asyncio
async def test_resource_pagination_is_stable_and_url_type_is_reserved(
    resource_env,
) -> None:
    env = resource_env
    for index in range(3):
        await _append_attachment(
            env,
            message_id=f"message-page-{index}",
            name=f"page-{index}.html",
            payload=f"<h1>{index}</h1>".encode(),
            staged=True,
        )

    first = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["attachment"], "limit": 1},
    )
    assert first.error is None, first.error
    assert first.payload["returnedCount"] == 1
    assert first.payload["hasMore"] is True
    assert isinstance(first.payload["nextCursor"], str)
    second = await _dispatch(
        env,
        "workbench.resources.list",
        {
            "sessionKey": SESSION_KEY,
            "types": ["attachment"],
            "limit": 1,
            "cursor": first.payload["nextCursor"],
        },
    )
    assert second.error is None, second.error
    assert second.payload["resources"][0]["resource"]["id"] != first.payload[
        "resources"
    ][0]["resource"]["id"]

    await _append_attachment(
        env,
        message_id="message-page-inventory-change",
        name="new.html",
        payload=b"<h1>new</h1>",
        staged=True,
    )
    stale = await _dispatch(
        env,
        "workbench.resources.list",
        {
            "sessionKey": SESSION_KEY,
            "types": ["attachment"],
            "limit": 1,
            "cursor": first.payload["nextCursor"],
        },
    )
    assert stale.error is not None
    assert stale.error.code == "DOCUMENT_CHANGED"
    assert stale.error.details == {"reasonCode": "resource_list_changed"}

    urls = await _dispatch(
        env,
        "workbench.resources.list",
        {"sessionKey": SESSION_KEY, "types": ["url"]},
    )
    assert urls.error is None, urls.error
    assert urls.payload["resources"] == []


@pytest.mark.parametrize(
    ("filename", "mime"),
    (
        (
            "brief.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "brief.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        (
            "brief.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ),
    ),
)
@pytest.mark.asyncio
async def test_office_resource_exposes_stable_edit_unavailable_reason(
    resource_env,
    filename: str,
    mime: str,
) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id=f"message-office-{filename}",
        name=filename,
        payload=b"synthetic-office-bytes",
        staged=True,
        mime=mime,
    )
    listed = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {"type": "attachment", "id": attachment["attachment_id"]},
        },
    )
    assert listed.error is None, listed.error
    capabilities = listed.payload["resource"]["capabilities"]
    assert capabilities["download"] is True
    assert capabilities["preview"] is False
    assert capabilities["selectionContext"] is False
    assert capabilities["manualEdit"] is False
    assert capabilities["agentEdit"] is False
    assert capabilities["edit"] is False
    assert capabilities["publish"] is False
    assert capabilities["editReasonCode"] == "office_adapter_not_available"

    forged_import = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "attachment", "id": attachment["attachment_id"]},
            "mode": "copy",
            "expectedSha256": attachment["sha256_ref"],
            "idempotencyKey": f"office-import-must-fail-closed-{filename}",
        },
    )
    assert forged_import.error is not None
    assert forged_import.error.code == "RESOURCE_UNSUPPORTED"
    assert forged_import.error.details == {"reasonCode": "office_adapter_not_available"}
    service = await ArtifactSessionService.from_session_storage(env.storage)
    assert await service.list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    ) == ()

    internal_ref = env.store.publish_bytes(
        b"synthetic-office-document",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name=filename,
        mime=mime,
        source="office-document-capability-test",
        visibility="internal",
    )
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        name=filename,
        kind=resource_rpc._kind_for(filename, mime),
        initial_artifact=ArtifactBlobRef(
            artifact_id=internal_ref.id,
            sha256=internal_ref.sha256,
            filename=internal_ref.name,
            media_type=internal_ref.mime,
            byte_size=internal_ref.size,
        ),
        actor=Actor(ActorKind.SYSTEM, "office-document-capability-test"),
    )
    described = await _dispatch(
        env,
        "workbench.resources.get",
        {
            "sessionKey": SESSION_KEY,
            "resource": {
                "type": "document",
                "id": created.document.document_id,
            },
        },
    )
    assert described.error is None, described.error
    document_capabilities = described.payload["resource"]["capabilities"]
    assert document_capabilities["download"] is True
    assert document_capabilities["preview"] is False
    assert document_capabilities["selectionContext"] is False
    assert document_capabilities["manualEdit"] is False
    assert document_capabilities["agentEdit"] is False
    assert document_capabilities["edit"] is False
    assert document_capabilities["publish"] is False

    rejected_publish = await _dispatch(
        env,
        "documents.publish",
        {
            "sessionKey": SESSION_KEY,
            "documentId": created.document.document_id,
            "revisionId": created.revision.revision_id,
            "idempotencyKey": f"office-publish-must-fail-closed-{filename}",
        },
    )
    assert rejected_publish.error is not None
    assert rejected_publish.error.code == "RESOURCE_UNSUPPORTED"
    assert rejected_publish.error.details == {"reasonCode": "office_adapter_not_available"}
    assert await service.list_document_publications(
        session_id=env.session.session_id,
        document_id=created.document.document_id,
    ) == ()


@pytest.mark.asyncio
async def test_publish_receipt_pins_immutable_revision_and_recovers_promotion(
    resource_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = resource_env
    emitted: list[tuple[str, str, dict[str, object]]] = []

    async def _capture_event(self, session_key, event_name, payload=None, **_kwargs):
        emitted.append((session_key, event_name, dict(payload or {})))

    monkeypatch.setattr(resource_rpc.EventBridge, "emit", _capture_event)
    source = b"<h1>published once</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-publish",
        name="publish.html",
        payload=source,
        staged=True,
    )
    imported = await _import_attachment(
        env,
        str(attachment["attachment_id"]),
        key="import-for-publish",
    )
    document_id = imported["document"]["id"]
    revision_id = imported["revision"]["id"]
    changes_before_missing_revision = env.storage.conn.total_changes
    missing_revision = await _dispatch(
        env,
        "documents.publish",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "clientRequestId": "publish-missing-revision",
        },
    )
    assert missing_revision.error is not None
    assert missing_revision.error.code == "INVALID_REQUEST"
    mismatched_aliases = await _dispatch(
        env,
        "documents.publish",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "revisionId": revision_id,
            "clientRequestId": "publish-alias-one",
            "idempotencyKey": "publish-alias-two",
        },
    )
    assert mismatched_aliases.error is not None
    assert mismatched_aliases.error.code == "INVALID_REQUEST"
    assert env.storage.conn.total_changes == changes_before_missing_revision

    original_promote = ArtifactStore.promote_internal_ref
    failed_once = False

    def _fail_first_promotion(self, **kwargs):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("synthetic promotion interruption")
        return original_promote(self, **kwargs)

    monkeypatch.setattr(ArtifactStore, "promote_internal_ref", _fail_first_promotion)
    params = {
        "sessionKey": SESSION_KEY,
        "documentId": document_id,
        "revisionId": revision_id,
        "clientRequestId": "publish-after-crash",
    }
    interrupted = await _dispatch(env, "documents.publish", params)
    assert interrupted.error is not None
    assert interrupted.error.code == "MUTATION_OUTCOME_PENDING"
    assert "promotion" not in interrupted.error.message.lower()
    assert "receipt" not in interrupted.error.message.lower()
    assert interrupted.error.details["correlationId"]
    assert env.store.list_refs(session_id=env.session.session_id, limit=10).refs == ()

    monkeypatch.setattr(ArtifactStore, "promote_internal_ref", original_promote)
    recovered = await _dispatch(env, "documents.publish", params)
    assert recovered.error is None, recovered.error
    assert recovered.payload["receipt"]["replayed"] is True
    assert recovered.payload["receipt"]["requestId"] == "publish-after-crash"
    assert recovered.payload["receipt"]["idempotencyKey"] == "publish-after-crash"
    publication = recovered.payload["publication"]
    assert publication["publicationId"] == publication["id"]
    assert publication["revisionId"] == revision_id
    assert publication["sha256"] == hashlib.sha256(source).hexdigest()
    deliverable_id = publication["deliverableId"]
    assert publication["artifactId"] == deliverable_id
    deliverable, path = env.store.resolve_for_download(
        deliverable_id,
        session_id=env.session.session_id,
    )
    assert Path(path).read_bytes() == source
    assert deliverable.sha256 == publication["sha256"]
    assert [event for _key, event, _payload in emitted] == [
        "session.event.artifact",
        "session.event.artifact_state",
        "document.state_changed",
    ]
    assert emitted[0][2]["id"] == deliverable_id
    assert emitted[1][2] == emitted[2][2]
    assert emitted[1][2]["action"] == "document.published"
    assert emitted[1][2]["documentId"] == document_id
    assert emitted[1][2]["revisionId"] == revision_id

    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "expectedHeadRevisionId": revision_id,
            "expectedStateRevision": imported["document"]["stateRevision"],
            "expectedSourceSha256": imported["revision"]["sha256"],
            "patches": [
                {"startOffset": 4, "endOffset": 18, "replacement": "changed"},
            ],
        },
    )
    assert patched.error is None, patched.error
    assert patched.payload["revision"]["sha256"] != publication["sha256"]

    documents_before_open = await (
        await ArtifactSessionService.from_session_storage(env.storage)
    ).list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    )
    opened = await _dispatch(
        env,
        "workbench.resources.open",
        {
            "sessionKey": SESSION_KEY,
            "resourceRef": {
                "type": "deliverable",
                "artifactId": deliverable_id,
            },
            "intent": "edit-current",
            "expectedSha256": publication["sha256"],
            "idempotencyKey": "open-published-original",
        },
    )
    assert opened.error is None, opened.error
    assert opened.payload["resolution"] == {"status": "current"}
    assert opened.payload["materialized"] is False
    assert opened.payload["document"]["documentId"] == document_id
    assert opened.payload["revision"]["revisionId"] == patched.payload["revision"]["id"]
    documents_after_open = await (
        await ArtifactSessionService.from_session_storage(env.storage)
    ).list_documents(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        limit=10,
    )
    assert documents_after_open == documents_before_open

    emitted_before_replay = len(emitted)
    replay = await _dispatch(
        env,
        "documents.publish",
        {**params, "idempotencyKey": "publish-after-crash"},
    )
    assert replay.error is None, replay.error
    assert len(emitted) == emitted_before_replay
    assert replay.payload["publication"] == publication
    _same_ref, same_path = env.store.resolve_for_download(
        deliverable_id,
        session_id=env.session.session_id,
    )
    assert Path(same_path).read_bytes() == source


@pytest.mark.asyncio
async def test_publish_explicitly_pins_non_head_revision_and_replays(resource_env) -> None:
    env = resource_env
    source = b"<h1>original</h1>"
    attachment = await _append_attachment(
        env,
        message_id="message-publish-non-head",
        name="non-head.html",
        payload=source,
        staged=True,
    )
    imported = await _import_attachment(
        env,
        str(attachment["attachment_id"]),
        key="import-for-non-head-publish",
    )
    original_revision = imported["revision"]
    document_id = imported["document"]["id"]
    patched = await _dispatch(
        env,
        "artifacts.source.patch",
        {
            "sessionKey": SESSION_KEY,
            "documentId": document_id,
            "expectedHeadRevisionId": original_revision["id"],
            "expectedStateRevision": imported["document"]["stateRevision"],
            "expectedSourceSha256": original_revision["sha256"],
            "patches": [
                {"startOffset": 4, "endOffset": 12, "replacement": "new head"},
            ],
        },
    )
    assert patched.error is None, patched.error
    assert patched.payload["revision"]["id"] != original_revision["id"]

    params = {
        "sessionKey": SESSION_KEY,
        "documentId": document_id,
        "revisionId": original_revision["id"],
        "clientRequestId": "publish-explicit-non-head",
    }
    published = await _dispatch(env, "documents.publish", params)
    assert published.error is None, published.error
    assert published.payload["publication"]["revisionId"] == original_revision["id"]
    assert published.payload["publication"]["sha256"] == original_revision["sha256"]
    _ref, path = env.store.resolve_for_download(
        published.payload["publication"]["deliverableId"],
        session_id=env.session.session_id,
    )
    assert Path(path).read_bytes() == source

    replay = await _dispatch(env, "documents.publish", params)
    assert replay.error is None, replay.error
    assert replay.payload["receipt"]["replayed"] is True
    assert replay.payload["publication"] == published.payload["publication"]


@pytest.mark.asyncio
async def test_session_reset_atomically_retires_applied_and_reserved_import_journals(
    resource_env,
) -> None:
    env = resource_env
    attachment = await _append_attachment(
        env,
        message_id="message-reset",
        name="reset.html",
        payload=b"<h1>reset</h1>",
        staged=True,
    )
    await _import_attachment(
        env,
        str(attachment["attachment_id"]),
        key="applied-before-reset",
    )
    service = await ArtifactSessionService.from_session_storage(env.storage)
    await service.reserve_document_import_attempt(
        session_key=SESSION_KEY,
        session_id=env.session.session_id,
        idempotency_key="reserved-before-reset",
        source_type=DocumentSourceType.ATTACHMENT,
        source_resource_id="att_reserved_before_reset",
        source_sha256="a" * 64,
        source_name="reserved.html",
        source_mime="text/html",
        source_size=1,
        document_name="reserved.html",
        mode=DocumentImportMode.COPY,
        candidate_artifact_id=ArtifactStore.allocate_artifact_id(),
    )

    old_session_id = env.session.session_id
    reset = await _dispatch(env, "sessions.reset", {"key": SESSION_KEY})
    assert reset.error is None, reset.error
    assert reset.payload["session_id"] != old_session_id

    for table in (
        "artifact_documents",
        "document_source_bindings",
        "document_import_attempts",
        "document_publications",
        "document_publish_attempts",
    ):
        cursor = await env.storage.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE session_id = ?",  # noqa: S608
            (old_session_id,),
        )
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        assert row is not None and int(row[0]) == 0, table


@pytest.mark.asyncio
async def test_legacy_document_open_preserves_preview_identity_and_is_idempotent(
    resource_env,
) -> None:
    env = resource_env
    ref = env.store.publish_bytes(
        b"<h1>legacy open</h1>",
        session_id=env.session.session_id,
        session_key=SESSION_KEY,
        name="legacy.html",
        mime="text/html",
        source="legacy-open-test",
    )
    params = {"sessionKey": SESSION_KEY, "artifactId": ref.id}
    first = await _dispatch(env, "artifacts.documents.open", params)
    second = await _dispatch(env, "artifacts.documents.open", params)
    assert first.error is None, first.error
    assert second.error is None, second.error
    assert first.payload["adopted"] is True
    assert second.payload["adopted"] is False
    assert first.payload["document"]["id"] == second.payload["document"]["id"]
    assert first.payload["document"]["head"]["artifactId"] == ref.id
    assert first.payload["document"]["head"]["sha256"] == ref.sha256

    imported = await _dispatch(
        env,
        "documents.import",
        {
            "sessionKey": SESSION_KEY,
            "source": {"type": "deliverable", "id": ref.id},
            "mode": "copy",
            "expectedSha256": ref.sha256,
            "idempotencyKey": "explicit-copy-after-legacy-open",
        },
    )
    assert imported.error is None, imported.error
    assert imported.payload["document"]["id"] != first.payload["document"]["id"]
    assert imported.payload["revision"]["artifactId"] != ref.id
    assert imported.payload["revision"]["sha256"] == ref.sha256
