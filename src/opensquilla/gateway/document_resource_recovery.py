"""Restart reconciliation for document import and publication journals."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    ArtifactSessionError,
    ArtifactSessionService,
    DocumentImportAttempt,
    DocumentPublishAttempt,
    DocumentSourceType,
    MutationAttemptStatus,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifacts import (
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactRef,
    ArtifactStore,
)
from opensquilla.paths import native_io_path

_HTML_MIMES = frozenset({"text/html", "application/xhtml+xml"})


@dataclass(frozen=True, slots=True)
class DocumentResourceRecoverySummary:
    imports_examined: int = 0
    imports_applied: int = 0
    imports_failed: int = 0
    imports_ambiguous: int = 0
    publishes_examined: int = 0
    publishes_applied: int = 0
    publishes_failed: int = 0
    publishes_ambiguous: int = 0
    deleted_candidates: int = 0
    promoted_deliverables: int = 0

    @property
    def examined(self) -> int:
        return self.imports_examined + self.publishes_examined


@dataclass(frozen=True, slots=True)
class DocumentImportRecoverySource:
    """Immutable source material re-resolved from one journaled resource identity."""

    session_key: str
    session_id: str
    source_type: DocumentSourceType
    resource_id: str
    name: str
    mime: str
    size: int
    sha256: str
    payload: bytes


DocumentImportSourceResolver = Callable[
    [DocumentImportAttempt],
    Awaitable[DocumentImportRecoverySource | None],
]


def _kind_for(name: str, mime: str) -> ArtifactKind:
    suffix = Path(name).suffix.casefold()
    normalized_mime = mime.casefold()
    if suffix in {".html", ".htm", ".xhtml"} or normalized_mime in _HTML_MIMES:
        return ArtifactKind.HTML
    if suffix == ".xlsx":
        return ArtifactKind.SPREADSHEET
    if suffix == ".pptx":
        return ArtifactKind.PRESENTATION
    if suffix == ".docx":
        return ArtifactKind.DOCUMENT
    return ArtifactKind.OTHER


def _candidate_blob(attempt: DocumentImportAttempt) -> ArtifactBlobRef:
    return ArtifactBlobRef(
        artifact_id=attempt.candidate_artifact_id,
        sha256=attempt.source_sha256,
        filename=attempt.document_name,
        media_type=attempt.source_mime,
        byte_size=attempt.source_size,
    )


def _verify_candidate(
    store: ArtifactStore,
    *,
    session_key: str,
    session_id: str,
    artifact_id: str,
    sha256: str,
    name: str,
    mime: str,
    size: int,
) -> ArtifactRef:
    ref, _path = store.resolve_for_download(artifact_id, session_id=session_id)
    if (
        ref.id != artifact_id
        or ref.session_key != session_key
        or ref.sha256 != sha256
        or ref.name != name
        or ref.mime != mime
        or ref.size != size
    ):
        raise ArtifactIntegrityError("journaled candidate does not match its receipt")
    return ref


def _payload_matches(payload: bytes, *, sha256: str, size: int) -> bool:
    return len(payload) == size and hashlib.sha256(payload).hexdigest() == sha256


def _materialize_internal_candidate(
    store: ArtifactStore,
    *,
    session_key: str,
    session_id: str,
    artifact_id: str,
    sha256: str,
    name: str,
    mime: str,
    size: int,
    payload: bytes,
    source: str,
) -> ArtifactRef:
    """Publish one preallocated candidate, repairing an owned partial bucket."""

    if not _payload_matches(payload, sha256=sha256, size=size):
        raise ArtifactIntegrityError("recovery source bytes do not match the journal")
    try:
        return _verify_candidate(
            store,
            session_key=session_key,
            session_id=session_id,
            artifact_id=artifact_id,
            sha256=sha256,
            name=name,
            mime=mime,
            size=size,
        )
    except ArtifactNotFoundError:
        pass

    for retry in range(2):
        try:
            store.publish_bytes(
                payload,
                session_id=session_id,
                session_key=session_key,
                name=name,
                mime=mime,
                source=source,
                visibility="internal",
                artifact_id=artifact_id,
            )
        except FileExistsError:
            try:
                return _verify_candidate(
                    store,
                    session_key=session_key,
                    session_id=session_id,
                    artifact_id=artifact_id,
                    sha256=sha256,
                    name=name,
                    mime=mime,
                    size=size,
                )
            except ArtifactNotFoundError:
                if retry:
                    raise
                # A hard crash can leave mkdir/marker/material without meta.json.
                # The store only deletes buckets whose exact ownership it proves.
                store.delete_reserved_bucket(
                    session_id=session_id,
                    artifact_id=artifact_id,
                )
                continue
        return _verify_candidate(
            store,
            session_key=session_key,
            session_id=session_id,
            artifact_id=artifact_id,
            sha256=sha256,
            name=name,
            mime=mime,
            size=size,
        )
    raise ArtifactNotFoundError("journaled candidate could not be materialized")


def _import_source_matches(
    attempt: DocumentImportAttempt,
    source: DocumentImportRecoverySource,
) -> bool:
    return (
        source.session_key == attempt.session_key
        and source.session_id == attempt.session_id
        and source.source_type == attempt.source_type
        and source.resource_id == attempt.source_resource_id
        and source.sha256 == attempt.source_sha256
        and source.name == attempt.source_name
        and source.mime == attempt.source_mime
        and source.size == attempt.source_size
        and _payload_matches(
            source.payload,
            sha256=attempt.source_sha256,
            size=attempt.source_size,
        )
    )


async def _restore_import_candidate(
    service: ArtifactSessionService,
    store: ArtifactStore,
    attempt: DocumentImportAttempt,
    source_resolver: DocumentImportSourceResolver | None,
) -> MutationAttemptStatus | None:
    """Restore a missing reserved candidate; ``None`` means recovery may commit."""

    if source_resolver is None:
        terminal = await service.fail_document_import_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_import_source_missing",
        )
        return terminal.status
    try:
        source = await source_resolver(attempt)
    except (ArtifactError, ArtifactSessionError, OSError, RuntimeError, ValueError):
        # Keep the journal RESERVED so a later restart can retry a transient read.
        return MutationAttemptStatus.AMBIGUOUS
    if source is None:
        terminal = await service.fail_document_import_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_import_source_missing",
        )
        return terminal.status
    if not _import_source_matches(attempt, source):
        terminal = await service.fail_document_import_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_import_source_mismatch",
        )
        return terminal.status
    try:
        await asyncio.to_thread(
            _materialize_internal_candidate,
            store,
            session_key=attempt.session_key,
            session_id=attempt.session_id,
            artifact_id=attempt.candidate_artifact_id,
            sha256=attempt.source_sha256,
            name=attempt.document_name,
            mime=attempt.source_mime,
            size=attempt.source_size,
            payload=source.payload,
            source="document-import-restart-recovery",
        )
    except (ArtifactError, OSError, ValueError):
        return MutationAttemptStatus.AMBIGUOUS
    return None


async def _restore_publish_candidate(
    service: ArtifactSessionService,
    store: ArtifactStore,
    attempt: DocumentPublishAttempt,
) -> MutationAttemptStatus | None:
    """Copy the immutable journaled Revision into its preallocated candidate."""

    try:
        document = await service.get_document(attempt.document_id)
        revision = await service.get_revision(attempt.revision_id)
    except ArtifactSessionNotFoundError:
        if attempt.status is not MutationAttemptStatus.RESERVED:
            return MutationAttemptStatus.AMBIGUOUS
        terminal = await service.fail_document_publish_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_publish_source_missing",
        )
        return terminal.status
    except ArtifactSessionError:
        return MutationAttemptStatus.AMBIGUOUS

    revision_matches = (
        document.document_id == attempt.document_id
        and document.session_key == attempt.session_key
        and document.session_id == attempt.session_id
        and revision.revision_id == attempt.revision_id
        and revision.document_id == document.document_id
        and revision.artifact_sha256 == attempt.artifact_sha256
        and revision.media_type == attempt.mime
        and revision.byte_size == attempt.size
    )
    if not revision_matches:
        if attempt.status is not MutationAttemptStatus.RESERVED:
            return MutationAttemptStatus.AMBIGUOUS
        terminal = await service.fail_document_publish_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_publish_source_mismatch",
        )
        return terminal.status

    try:
        source_ref, source_path = await asyncio.to_thread(
            store.resolve_for_download,
            revision.artifact_id,
            session_id=attempt.session_id,
        )
    except ArtifactNotFoundError:
        if attempt.status is not MutationAttemptStatus.RESERVED:
            return MutationAttemptStatus.AMBIGUOUS
        terminal = await service.fail_document_publish_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_publish_source_missing",
        )
        return terminal.status
    except ArtifactIntegrityError:
        if attempt.status is not MutationAttemptStatus.RESERVED:
            return MutationAttemptStatus.AMBIGUOUS
        terminal = await service.fail_document_publish_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_publish_source_mismatch",
        )
        return terminal.status
    except (ArtifactError, OSError, ValueError):
        return MutationAttemptStatus.AMBIGUOUS

    source_matches = (
        source_ref.id == revision.artifact_id
        and source_ref.session_id == attempt.session_id
        and source_ref.session_key == attempt.session_key
        and source_ref.sha256 == revision.artifact_sha256
        and source_ref.name == revision.filename
        and source_ref.mime == revision.media_type
        and source_ref.size == revision.byte_size
    )
    if not source_matches:
        if attempt.status is not MutationAttemptStatus.RESERVED:
            return MutationAttemptStatus.AMBIGUOUS
        terminal = await service.fail_document_publish_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_publish_source_mismatch",
        )
        return terminal.status
    try:
        payload = await asyncio.to_thread(native_io_path(source_path).read_bytes)
    except OSError:
        return MutationAttemptStatus.AMBIGUOUS
    if not _payload_matches(
        payload,
        sha256=revision.artifact_sha256,
        size=revision.byte_size,
    ):
        if attempt.status is not MutationAttemptStatus.RESERVED:
            return MutationAttemptStatus.AMBIGUOUS
        terminal = await service.fail_document_publish_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_publish_source_mismatch",
        )
        return terminal.status
    try:
        await asyncio.to_thread(
            _materialize_internal_candidate,
            store,
            session_key=attempt.session_key,
            session_id=attempt.session_id,
            artifact_id=attempt.candidate_artifact_id,
            sha256=attempt.artifact_sha256,
            name=attempt.name,
            mime=attempt.mime,
            size=attempt.size,
            payload=payload,
            source="document-publish-restart-recovery",
        )
    except (ArtifactError, OSError, ValueError):
        return MutationAttemptStatus.AMBIGUOUS
    return None


async def _delete_import_candidate(
    service: ArtifactSessionService,
    store: ArtifactStore,
    attempt: DocumentImportAttempt,
) -> bool:
    deleted = await asyncio.to_thread(
        store.delete_ref,
        session_id=attempt.session_id,
        artifact_id=attempt.candidate_artifact_id,
    )
    if not deleted:
        try:
            await asyncio.to_thread(
                store.resolve_for_download,
                attempt.candidate_artifact_id,
                session_id=attempt.session_id,
            )
        except ArtifactNotFoundError:
            pass
        else:
            raise ArtifactError("unused import candidate still exists")
    await service.mark_document_import_candidate_cleaned(
        session_id=attempt.session_id,
        idempotency_key=attempt.idempotency_key,
    )
    return deleted


async def _recover_import(
    service: ArtifactSessionService,
    store: ArtifactStore,
    attempt: DocumentImportAttempt,
    source_resolver: DocumentImportSourceResolver | None,
) -> tuple[MutationAttemptStatus, bool]:
    if attempt.status is MutationAttemptStatus.APPLIED:
        try:
            result = await service.apply_document_import_attempt(
                session_id=attempt.session_id,
                idempotency_key=attempt.idempotency_key,
                candidate_artifact=_candidate_blob(attempt),
                document_name=attempt.document_name,
                kind=_kind_for(attempt.document_name, attempt.source_mime),
                actor=Actor(kind=ActorKind.SYSTEM, actor_id="restart-recovery"),
            )
            if result.commit.revision.artifact_id == attempt.candidate_artifact_id:
                return MutationAttemptStatus.APPLIED, False
            deleted = await _delete_import_candidate(service, store, attempt)
            return MutationAttemptStatus.APPLIED, deleted
        except (ArtifactError, ArtifactSessionError, OSError, ValueError):
            return MutationAttemptStatus.AMBIGUOUS, False

    try:
        await asyncio.to_thread(
            _verify_candidate,
            store,
            session_key=attempt.session_key,
            session_id=attempt.session_id,
            artifact_id=attempt.candidate_artifact_id,
            sha256=attempt.source_sha256,
            name=attempt.document_name,
            mime=attempt.source_mime,
            size=attempt.source_size,
        )
    except ArtifactNotFoundError:
        restore_status = await _restore_import_candidate(
            service,
            store,
            attempt,
            source_resolver,
        )
        if restore_status is not None:
            return restore_status, False
    except (ArtifactError, ArtifactSessionError, OSError, ValueError):
        terminal = await service.fail_document_import_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_import_candidate_invalid",
            ambiguous=True,
        )
        return terminal.status, False

    try:
        result = await service.apply_document_import_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            candidate_artifact=_candidate_blob(attempt),
            document_name=attempt.document_name,
            kind=_kind_for(attempt.document_name, attempt.source_mime),
            actor=Actor(kind=ActorKind.SYSTEM, actor_id="restart-recovery"),
        )
        if result.commit.revision.artifact_id != attempt.candidate_artifact_id:
            deleted = await _delete_import_candidate(service, store, attempt)
            return MutationAttemptStatus.APPLIED, deleted
        return MutationAttemptStatus.APPLIED, False
    except (ArtifactError, ArtifactSessionError, OSError, ValueError):
        terminal = await service.fail_document_import_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            failure_code="restart_import_commit_ambiguous",
            ambiguous=True,
        )
        return terminal.status, False


async def _recover_publish(
    service: ArtifactSessionService,
    store: ArtifactStore,
    attempt: DocumentPublishAttempt,
) -> tuple[MutationAttemptStatus, bool]:
    try:
        await asyncio.to_thread(
            _verify_candidate,
            store,
            session_key=attempt.session_key,
            session_id=attempt.session_id,
            artifact_id=attempt.candidate_artifact_id,
            sha256=attempt.artifact_sha256,
            name=attempt.name,
            mime=attempt.mime,
            size=attempt.size,
        )
    except ArtifactNotFoundError:
        restore_status = await _restore_publish_candidate(service, store, attempt)
        if restore_status is not None:
            return restore_status, False
    except (ArtifactError, ArtifactSessionError, OSError, ValueError):
        if attempt.status is MutationAttemptStatus.RESERVED:
            terminal = await service.fail_document_publish_attempt(
                session_id=attempt.session_id,
                idempotency_key=attempt.idempotency_key,
                failure_code="restart_publish_candidate_invalid",
                ambiguous=True,
            )
            return terminal.status, False
        return MutationAttemptStatus.AMBIGUOUS, False

    try:
        result = await service.apply_document_publish_attempt(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
            actor=Actor(kind=ActorKind.SYSTEM, actor_id="restart-recovery"),
        )
        await asyncio.to_thread(
            store.promote_internal_ref,
            session_id=attempt.session_id,
            artifact_id=result.publication.deliverable_artifact_id,
            expected_sha256=result.publication.artifact_sha256,
        )
        await service.mark_document_publish_promoted(
            session_id=attempt.session_id,
            idempotency_key=attempt.idempotency_key,
        )
        return MutationAttemptStatus.APPLIED, True
    except (ArtifactError, ArtifactSessionError, OSError, ValueError):
        if attempt.status is MutationAttemptStatus.RESERVED:
            # The database transaction may already have committed. Preserve the
            # applied row for a later idempotent promotion instead of guessing.
            refreshed = await service.get_document_publish_attempt(
                session_id=attempt.session_id,
                idempotency_key=attempt.idempotency_key,
            )
            if refreshed.status is MutationAttemptStatus.RESERVED:
                terminal = await service.fail_document_publish_attempt(
                    session_id=attempt.session_id,
                    idempotency_key=attempt.idempotency_key,
                    failure_code="restart_publish_commit_ambiguous",
                    ambiguous=True,
                )
                return terminal.status, False
        return MutationAttemptStatus.AMBIGUOUS, False


def _merge_import(
    summary: DocumentResourceRecoverySummary,
    status: MutationAttemptStatus,
    deleted: bool,
) -> DocumentResourceRecoverySummary:
    return DocumentResourceRecoverySummary(
        imports_examined=summary.imports_examined + 1,
        imports_applied=summary.imports_applied + (status is MutationAttemptStatus.APPLIED),
        imports_failed=summary.imports_failed + (status is MutationAttemptStatus.FAILED),
        imports_ambiguous=summary.imports_ambiguous
        + (status is MutationAttemptStatus.AMBIGUOUS),
        publishes_examined=summary.publishes_examined,
        publishes_applied=summary.publishes_applied,
        publishes_failed=summary.publishes_failed,
        publishes_ambiguous=summary.publishes_ambiguous,
        deleted_candidates=summary.deleted_candidates + deleted,
        promoted_deliverables=summary.promoted_deliverables,
    )


def _merge_publish(
    summary: DocumentResourceRecoverySummary,
    status: MutationAttemptStatus,
    promoted: bool,
) -> DocumentResourceRecoverySummary:
    return DocumentResourceRecoverySummary(
        imports_examined=summary.imports_examined,
        imports_applied=summary.imports_applied,
        imports_failed=summary.imports_failed,
        imports_ambiguous=summary.imports_ambiguous,
        publishes_examined=summary.publishes_examined + 1,
        publishes_applied=summary.publishes_applied + (status is MutationAttemptStatus.APPLIED),
        publishes_failed=summary.publishes_failed + (status is MutationAttemptStatus.FAILED),
        publishes_ambiguous=summary.publishes_ambiguous
        + (status is MutationAttemptStatus.AMBIGUOUS),
        deleted_candidates=summary.deleted_candidates,
        promoted_deliverables=summary.promoted_deliverables + promoted,
    )


async def reconcile_pending_document_resources(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    batch_size: int = 100,
    import_source_resolver: DocumentImportSourceResolver | None = None,
) -> DocumentResourceRecoverySummary:
    """Recover every pending import and publication without client retries."""

    summary = DocumentResourceRecoverySummary()
    after: str | None = None
    while True:
        import_attempts = await service.list_document_import_attempts_for_recovery(
            limit=batch_size,
            after_attempt_id=after,
        )
        if not import_attempts:
            break
        for import_attempt in import_attempts:
            status, deleted = await _recover_import(
                service,
                store,
                import_attempt,
                import_source_resolver,
            )
            summary = _merge_import(summary, status, deleted)
        after = import_attempts[-1].attempt_id

    after = None
    while True:
        publish_attempts = await service.list_document_publish_attempts_for_recovery(
            limit=batch_size,
            after_attempt_id=after,
        )
        if not publish_attempts:
            return summary
        for publish_attempt in publish_attempts:
            status, promoted = await _recover_publish(service, store, publish_attempt)
            summary = _merge_publish(summary, status, promoted)
        after = publish_attempts[-1].attempt_id


__all__ = [
    "DocumentImportRecoverySource",
    "DocumentImportSourceResolver",
    "DocumentResourceRecoverySummary",
    "reconcile_pending_document_resources",
]
