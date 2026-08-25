"""Process-restart recovery for document resource journals."""

from __future__ import annotations

import base64
import hashlib
import json
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
    DocumentImportAttempt,
    DocumentImportMode,
    DocumentSourceType,
    MutationAttemptStatus,
)
from opensquilla.artifacts import ArtifactNotFoundError, ArtifactStore
from opensquilla.gateway.document_resource_recovery import (
    DocumentImportRecoverySource,
    reconcile_pending_document_resources,
)
from opensquilla.gateway.rpc import RpcContext
from opensquilla.gateway.transcripts import build_transcript_attachment_envelope
from opensquilla.session.manager import SessionManager
from opensquilla.session.models import TranscriptEntry
from opensquilla.session.storage import SessionStorage

SESSION_KEY = "agent:main:webchat:document-resource-recovery"
SESSION_ID = "document-resource-recovery-session"
SYSTEM = Actor(ActorKind.SYSTEM, "test-system")


def _blob(ref) -> ArtifactBlobRef:
    return ArtifactBlobRef(
        artifact_id=ref.id,
        sha256=ref.sha256,
        filename=ref.name,
        media_type=ref.mime,
        byte_size=ref.size,
    )


def _publish_internal(
    store: ArtifactStore,
    *,
    payload: bytes,
    name: str,
    artifact_id: str,
    session_key: str = SESSION_KEY,
    session_id: str = SESSION_ID,
):
    return store.publish_bytes(
        payload,
        session_id=session_id,
        session_key=session_key,
        name=name,
        mime="text/html",
        source="document-resource-recovery-test",
        visibility="internal",
        artifact_id=artifact_id,
    )


def _config(tmp_path: Path, media_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        attachments=SimpleNamespace(
            media_root=str(media_root),
            persist_transcripts=True,
        ),
        state_dir=str(tmp_path / "state"),
        config_path=None,
    )


def _import_source_resolver(ctx: RpcContext):
    async def resolve(attempt: DocumentImportAttempt):
        return await resource_rpc.resolve_recovery_import_source(ctx, attempt)

    return resolve


async def _reserve_import(
    service: ArtifactSessionService,
    ref,
    *,
    idempotency_key: str,
    source_resource_id: str,
):
    return await service.reserve_document_import_attempt(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        idempotency_key=idempotency_key,
        source_type=DocumentSourceType.ATTACHMENT,
        source_resource_id=source_resource_id,
        source_sha256=ref.sha256,
        source_name=ref.name,
        source_mime=ref.mime,
        source_size=ref.size,
        document_name=ref.name,
        mode=DocumentImportMode.COPY,
        candidate_artifact_id=ref.id,
    )


@pytest.mark.parametrize("staged", [False, True], ids=["inline", "staged"])
@pytest.mark.asyncio
async def test_restart_materializes_missing_import_candidate_from_attachment(
    tmp_path: Path,
    staged: bool,
) -> None:
    db_path = tmp_path / "sessions.db"
    media_root = tmp_path / "media"
    config = _config(tmp_path, media_root)
    payload = b"<!doctype html><h1>attachment restart</h1>"

    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    session = await manager.create(SESSION_KEY)
    attachment = {
        "type": "application/octet-stream",
        "data": base64.b64encode(payload).decode("ascii"),
        "name": "attachment.html",
    }
    if staged:
        attachment["_was_staged"] = True
    envelope, _writes = build_transcript_attachment_envelope(
        text="uploaded attachment.html",
        attachments=[attachment],
        session_id=session.session_id,
        media_root=media_root,
        persist_enabled=True,
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=session.session_id,
            session_key=SESSION_KEY,
            message_id=f"attachment-restart-{staged}",
            role="user",
            content=envelope,
        )
    )
    attachment_id = str(json.loads(envelope)["attachments"][0]["attachment_id"])
    context = RpcContext(
        conn_id="document-resource-recovery-setup",
        session_manager=manager,
        config=config,
    )
    source = await resource_rpc._resolve_import_source(
        context,
        session_key=SESSION_KEY,
        session_id=session.session_id,
        source_type="attachment",
        resource_id=attachment_id,
    )
    assert source.mime == "application/octet-stream"
    source, _adapter = resource_rpc._validated_import_source(source)
    assert source.mime == "text/html"
    candidate_id = ArtifactStore.allocate_artifact_id()
    service = await ArtifactSessionService.from_session_storage(storage)
    await service.reserve_document_import_attempt(
        session_key=SESSION_KEY,
        session_id=session.session_id,
        idempotency_key=f"attachment-reserve-before-copy-{staged}",
        source_type=DocumentSourceType.ATTACHMENT,
        source_resource_id=attachment_id,
        source_sha256=source.sha256,
        source_name=source.name,
        source_mime=source.mime,
        source_size=source.size,
        document_name="working-copy.html",
        mode=DocumentImportMode.COPY,
        candidate_artifact_id=candidate_id,
    )
    await service.close()
    await storage.close()

    reopened_storage = SessionStorage(str(db_path))
    await reopened_storage.connect()
    reopened_manager = SessionManager(
        reopened_storage,
        inject_time_prefix=False,
        media_root=media_root,
    )
    reopened_context = RpcContext(
        conn_id="document-resource-recovery-reopen",
        session_manager=reopened_manager,
        config=config,
    )
    recovered = await ArtifactSessionService.from_session_storage(reopened_storage)
    store = ArtifactStore(media_root)
    try:
        summary = await reconcile_pending_document_resources(
            recovered,
            store,
            import_source_resolver=_import_source_resolver(reopened_context),
        )
        assert summary.imports_applied == 1
        candidate, candidate_path = store.resolve_for_download(
            candidate_id,
            session_id=session.session_id,
        )
        assert candidate.name == "working-copy.html"
        assert candidate.sha256 == hashlib.sha256(payload).hexdigest()
        assert candidate_path.read_bytes() == payload
        documents = await recovered.list_documents(
            session_key=SESSION_KEY,
            session_id=session.session_id,
        )
        assert len(documents) == 1
        revision = await recovered.get_revision(documents[0].head_revision_id)
        assert revision.artifact_id == candidate_id
    finally:
        await recovered.close()
        await reopened_storage.close()


@pytest.mark.asyncio
async def test_restart_materializes_missing_import_candidate_from_deliverable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    media_root = tmp_path / "media"
    config = _config(tmp_path, media_root)
    payload = b"<!doctype html><h1>deliverable restart</h1>"

    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    session = await manager.create(SESSION_KEY)
    store = ArtifactStore(media_root)
    deliverable = store.publish_bytes(
        payload,
        session_id=session.session_id,
        session_key=SESSION_KEY,
        name="published-source.html",
        mime="text/html",
        source="document-resource-recovery-test",
    )
    context = RpcContext(
        conn_id="document-resource-recovery-setup",
        session_manager=manager,
        config=config,
    )
    source = await resource_rpc._resolve_import_source(
        context,
        session_key=SESSION_KEY,
        session_id=session.session_id,
        source_type="deliverable",
        resource_id=deliverable.id,
    )
    candidate_id = ArtifactStore.allocate_artifact_id()
    service = await ArtifactSessionService.from_session_storage(storage)
    await service.reserve_document_import_attempt(
        session_key=SESSION_KEY,
        session_id=session.session_id,
        idempotency_key="deliverable-reserve-before-copy",
        source_type=DocumentSourceType.DELIVERABLE,
        source_resource_id=deliverable.id,
        source_sha256=source.sha256,
        source_name=source.name,
        source_mime=source.mime,
        source_size=source.size,
        document_name="deliverable-working-copy.html",
        mode=DocumentImportMode.COPY,
        candidate_artifact_id=candidate_id,
    )
    await service.close()
    await storage.close()

    reopened_storage = SessionStorage(str(db_path))
    await reopened_storage.connect()
    reopened_manager = SessionManager(
        reopened_storage,
        inject_time_prefix=False,
        media_root=media_root,
    )
    reopened_context = RpcContext(
        conn_id="document-resource-recovery-reopen",
        session_manager=reopened_manager,
        config=config,
    )
    recovered = await ArtifactSessionService.from_session_storage(reopened_storage)
    try:
        summary = await reconcile_pending_document_resources(
            recovered,
            store,
            import_source_resolver=_import_source_resolver(reopened_context),
        )
        assert summary.imports_applied == 1
        candidate, candidate_path = store.resolve_for_download(
            candidate_id,
            session_id=session.session_id,
        )
        assert candidate.name == "deliverable-working-copy.html"
        assert candidate_path.read_bytes() == payload
        assert [
            item.id
            for item in store.list_refs(session_id=session.session_id, limit=100).refs
        ] == [deliverable.id]
        documents = await recovered.list_documents(
            session_key=SESSION_KEY,
            session_id=session.session_id,
        )
        assert len(documents) == 1
    finally:
        await recovered.close()
        await reopened_storage.close()


@pytest.mark.parametrize(
    "payload",
    [b"\xff<html></html>", b"<html>\x00</html>"],
    ids=["invalid-utf8", "nul-validation"],
)
@pytest.mark.asyncio
async def test_restart_does_not_bypass_import_source_validation(
    tmp_path: Path,
    payload: bytes,
) -> None:
    db_path = tmp_path / "sessions.db"
    media_root = tmp_path / "media"
    config = _config(tmp_path, media_root)
    storage = SessionStorage(str(db_path))
    await storage.connect()
    manager = SessionManager(storage, inject_time_prefix=False, media_root=media_root)
    session = await manager.create(SESSION_KEY)
    envelope, _writes = build_transcript_attachment_envelope(
        text="uploaded invalid.html",
        attachments=[
            {
                "type": "application/octet-stream",
                "data": base64.b64encode(payload).decode("ascii"),
                "name": "invalid.html",
            }
        ],
        session_id=session.session_id,
        media_root=media_root,
        persist_enabled=True,
    )
    await storage.append_transcript_entry(
        TranscriptEntry(
            session_id=session.session_id,
            session_key=SESSION_KEY,
            message_id="invalid-recovery-source",
            role="user",
            content=envelope,
        )
    )
    attachment_id = str(json.loads(envelope)["attachments"][0]["attachment_id"])
    candidate_id = ArtifactStore.allocate_artifact_id()
    service = await ArtifactSessionService.from_session_storage(storage)
    await service.reserve_document_import_attempt(
        session_key=SESSION_KEY,
        session_id=session.session_id,
        idempotency_key="invalid-reserve-before-copy",
        source_type=DocumentSourceType.ATTACHMENT,
        source_resource_id=attachment_id,
        source_sha256=hashlib.sha256(payload).hexdigest(),
        source_name="invalid.html",
        source_mime="text/html",
        source_size=len(payload),
        document_name="invalid.html",
        mode=DocumentImportMode.COPY,
        candidate_artifact_id=candidate_id,
    )
    await service.close()
    await storage.close()

    reopened_storage = SessionStorage(str(db_path))
    await reopened_storage.connect()
    reopened_manager = SessionManager(
        reopened_storage,
        inject_time_prefix=False,
        media_root=media_root,
    )
    reopened_context = RpcContext(
        conn_id="document-resource-invalid-reopen",
        session_manager=reopened_manager,
        config=config,
    )
    recovered = await ArtifactSessionService.from_session_storage(reopened_storage)
    store = ArtifactStore(media_root)
    try:
        summary = await reconcile_pending_document_resources(
            recovered,
            store,
            import_source_resolver=_import_source_resolver(reopened_context),
        )
        assert summary.imports_failed == 1
        attempt = await recovered.get_document_import_attempt(
            session_id=session.session_id,
            idempotency_key="invalid-reserve-before-copy",
        )
        assert attempt.status is MutationAttemptStatus.FAILED
        assert attempt.failure_code == "restart_import_source_missing"
        assert await recovered.list_documents(
            session_key=SESSION_KEY,
            session_id=session.session_id,
        ) == ()
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(candidate_id, session_id=session.session_id)
    finally:
        await recovered.close()
        await reopened_storage.close()


@pytest.mark.asyncio
async def test_restart_applies_reserved_import_without_client_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = ArtifactStore(tmp_path / "media")
    ref = _publish_internal(
        store,
        payload=b"<h1>restart import</h1>",
        name="restart.html",
        artifact_id="art-restart-import",
    )
    service = await ArtifactSessionService.open(db_path)
    await _reserve_import(
        service,
        ref,
        idempotency_key="import-after-restart",
        source_resource_id="att_restart_import",
    )
    await service.close()

    recovered = await ArtifactSessionService.open(db_path)
    try:
        summary = await reconcile_pending_document_resources(recovered, store)
        assert summary.imports_examined == 1
        assert summary.imports_applied == 1
        attempt = await recovered.get_document_import_attempt(
            session_id=SESSION_ID,
            idempotency_key="import-after-restart",
        )
        assert attempt.status is MutationAttemptStatus.APPLIED
        assert len(
            await recovered.list_documents(
                session_key=SESSION_KEY,
                session_id=SESSION_ID,
            )
        ) == 1
        assert (await reconcile_pending_document_resources(recovered, store)).examined == 0
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_restart_cleans_unused_import_candidate_and_records_completion(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    store = ArtifactStore(tmp_path / "media")
    payload = b"<h1>same occurrence</h1>"
    first_ref = _publish_internal(
        store,
        payload=payload,
        name="same.html",
        artifact_id="art-import-first",
    )
    unused_ref = _publish_internal(
        store,
        payload=payload,
        name="same.html",
        artifact_id="art-import-unused",
    )
    service = await ArtifactSessionService.open(db_path)
    await _reserve_import(
        service,
        first_ref,
        idempotency_key="first-import",
        source_resource_id="att_same_occurrence",
    )
    await service.apply_document_import_attempt(
        session_id=SESSION_ID,
        idempotency_key="first-import",
        candidate_artifact=_blob(first_ref),
        document_name=first_ref.name,
        kind=ArtifactKind.HTML,
        actor=SYSTEM,
    )
    await _reserve_import(
        service,
        unused_ref,
        idempotency_key="replayed-source-import",
        source_resource_id="att_same_occurrence",
    )
    reused = await service.apply_document_import_attempt(
        session_id=SESSION_ID,
        idempotency_key="replayed-source-import",
        candidate_artifact=_blob(unused_ref),
        document_name=unused_ref.name,
        kind=ArtifactKind.HTML,
        actor=SYSTEM,
    )
    assert reused.commit.revision.artifact_id == first_ref.id
    await service.close()

    recovered = await ArtifactSessionService.open(db_path)
    try:
        summary = await reconcile_pending_document_resources(recovered, store)
        assert summary.deleted_candidates == 1
        with pytest.raises(ArtifactNotFoundError):
            store.resolve_for_download(unused_ref.id, session_id=SESSION_ID)
        attempt = await recovered.get_document_import_attempt(
            session_id=SESSION_ID,
            idempotency_key="replayed-source-import",
        )
        assert attempt.candidate_cleaned_at is not None
        assert (await reconcile_pending_document_resources(recovered, store)).examined == 0
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_restart_promotes_db_committed_publication_once(tmp_path: Path) -> None:
    db_path = tmp_path / "sessions.db"
    store = ArtifactStore(tmp_path / "media")
    head_ref = _publish_internal(
        store,
        payload=b"<h1>immutable head</h1>",
        name="page.html",
        artifact_id="art-internal-head",
    )
    service = await ArtifactSessionService.open(db_path)
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="page.html",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob(head_ref),
        actor=SYSTEM,
    )
    publish_ref = _publish_internal(
        store,
        payload=b"<h1>immutable head</h1>",
        name="published.html",
        artifact_id="art-publish-after-restart",
    )
    assert publish_ref.sha256 == hashlib.sha256(b"<h1>immutable head</h1>").hexdigest()
    await service.reserve_document_publish_attempt(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        idempotency_key="publish-after-restart",
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        candidate_artifact=_blob(publish_ref),
    )
    await service.apply_document_publish_attempt(
        session_id=SESSION_ID,
        idempotency_key="publish-after-restart",
        actor=SYSTEM,
    )
    await service.close()

    recovered = await ArtifactSessionService.open(db_path)
    try:
        summary = await reconcile_pending_document_resources(recovered, store)
        assert summary.publishes_examined == 1
        assert summary.publishes_applied == 1
        assert summary.promoted_deliverables == 1
        attempt = await recovered.get_document_publish_attempt(
            session_id=SESSION_ID,
            idempotency_key="publish-after-restart",
        )
        assert attempt.promoted_at is not None
        assert [
            item.id for item in store.list_refs(session_id=SESSION_ID, limit=100).refs
        ] == [
            publish_ref.id
        ]
        assert (await reconcile_pending_document_resources(recovered, store)).examined == 0
        assert len(await recovered.list_document_publications(session_id=SESSION_ID)) == 1
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_restart_copies_revision_when_publish_candidate_was_not_written(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "sessions.db"
    store = ArtifactStore(tmp_path / "media")
    payload = b"<h1>reserve before publish copy</h1>"
    head_ref = _publish_internal(
        store,
        payload=payload,
        name="source-head.html",
        artifact_id="art-publish-source-head",
    )
    candidate_id = "art-publish-missing-candidate"
    service = await ArtifactSessionService.open(db_path)
    created = await service.create_document(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        name="source-head.html",
        kind=ArtifactKind.HTML,
        initial_artifact=_blob(head_ref),
        actor=SYSTEM,
    )
    await service.reserve_document_publish_attempt(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        idempotency_key="publish-reserve-before-copy",
        document_id=created.document.document_id,
        revision_id=created.revision.revision_id,
        candidate_artifact=ArtifactBlobRef(
            artifact_id=candidate_id,
            sha256=head_ref.sha256,
            filename="immutable-release.html",
            media_type=head_ref.mime,
            byte_size=head_ref.size,
        ),
    )
    with pytest.raises(ArtifactNotFoundError):
        store.resolve_for_download(candidate_id, session_id=SESSION_ID)
    await service.close()

    recovered = await ArtifactSessionService.open(db_path)
    try:
        summary = await reconcile_pending_document_resources(recovered, store)
        assert summary.publishes_applied == 1
        assert summary.promoted_deliverables == 1
        candidate, candidate_path = store.resolve_for_download(
            candidate_id,
            session_id=SESSION_ID,
        )
        assert candidate.name == "immutable-release.html"
        assert candidate.sha256 == head_ref.sha256
        assert candidate_path.read_bytes() == payload
        assert [
            item.id for item in store.list_refs(session_id=SESSION_ID, limit=100).refs
        ] == [candidate_id]
        publications = await recovered.list_document_publications(session_id=SESSION_ID)
        assert len(publications) == 1
        assert publications[0].revision_id == created.revision.revision_id
        assert publications[0].deliverable_artifact_id == candidate_id
    finally:
        await recovered.close()


@pytest.mark.asyncio
async def test_restart_terminalizes_missing_reserved_candidates(tmp_path: Path) -> None:
    service = await ArtifactSessionService.open(tmp_path / "sessions.db")
    store = ArtifactStore(tmp_path / "media")
    payload = b"<h1>missing</h1>"
    digest = hashlib.sha256(payload).hexdigest()
    await service.reserve_document_import_attempt(
        session_key=SESSION_KEY,
        session_id=SESSION_ID,
        idempotency_key="missing-import",
        source_type=DocumentSourceType.ATTACHMENT,
        source_resource_id="att_missing",
        source_sha256=digest,
        source_name="missing.html",
        source_mime="text/html",
        source_size=len(payload),
        document_name="missing.html",
        mode=DocumentImportMode.COPY,
        candidate_artifact_id="art-missing-import",
    )
    await service.close()

    recovered = await ArtifactSessionService.open(tmp_path / "sessions.db")
    try:
        async def missing_source(
            _attempt: DocumentImportAttempt,
        ) -> DocumentImportRecoverySource | None:
            return None

        summary = await reconcile_pending_document_resources(
            recovered,
            store,
            import_source_resolver=missing_source,
        )
        assert summary.imports_failed == 1
        attempt = await recovered.get_document_import_attempt(
            session_id=SESSION_ID,
            idempotency_key="missing-import",
        )
        assert attempt.status is MutationAttemptStatus.FAILED
        assert attempt.failure_code == "restart_import_source_missing"
    finally:
        await recovered.close()
