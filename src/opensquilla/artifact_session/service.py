"""Validated application service for durable ArtifactSession operations."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .errors import ArtifactValidationError
from .models import (
    Actor,
    ActorKind,
    Anchor,
    AnchorKind,
    AnchorState,
    ArtifactBlobRef,
    ArtifactKind,
    AuditEvent,
    ChangeSet,
    ChangeSetStatus,
    CommitResult,
    Document,
    DocumentImportAttempt,
    DocumentImportMode,
    DocumentImportResult,
    DocumentPublication,
    DocumentPublishAttempt,
    DocumentPublishResult,
    DocumentSourceBinding,
    DocumentSourceType,
    EditSession,
    MutationAttempt,
    PromptAnnotation,
    PromptAnnotationStatus,
    Revision,
    RevisionSource,
    WriterLease,
)
from .repository import ArtifactSessionRepository, Clock, IdFactory, _SessionStorageBinding

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_FAILURE_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_PROMPT_ANNOTATION_BODY_BYTES = 16_384


def _required(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ArtifactValidationError(f"{field} must not be empty")
    return normalized


def _bounded_text(value: str, field: str, *, max_bytes: int) -> str:
    normalized = _required(value, field)
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ArtifactValidationError(f"{field} is too long")
    return normalized


def _positive(value: int, field: str) -> int:
    if isinstance(value, bool) or value <= 0:
        raise ArtifactValidationError(f"{field} must be a positive integer")
    return value


def _nonnegative(value: int, field: str) -> int:
    if isinstance(value, bool) or value < 0:
        raise ArtifactValidationError(f"{field} must be a non-negative integer")
    return value


def _bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not 1 <= value <= 1000:
        raise ArtifactValidationError("limit must be between 1 and 1000")
    return value


def _actor(actor: Actor) -> Actor:
    return Actor(kind=actor.kind, actor_id=_required(actor.actor_id, "actor_id"))


def _blob(blob: ArtifactBlobRef) -> ArtifactBlobRef:
    artifact_id = _required(blob.artifact_id, "artifact_id")
    sha256 = _required(blob.sha256, "sha256")
    if not _SHA256_RE.fullmatch(sha256):
        raise ArtifactValidationError("sha256 must contain exactly 64 hexadecimal characters")
    return ArtifactBlobRef(
        artifact_id=artifact_id,
        sha256=sha256.lower(),
        filename=_required(blob.filename, "filename"),
        media_type=_required(blob.media_type, "media_type"),
        byte_size=_nonnegative(blob.byte_size, "byte_size"),
    )


def _json_value(value: object, field: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"{field} must be finite JSON data") from exc


def _prompt_annotation_body(value: str) -> str:
    if not isinstance(value, str):
        raise ArtifactValidationError("body must be a string")
    if len(value.encode("utf-8")) > _MAX_PROMPT_ANNOTATION_BODY_BYTES:
        raise ArtifactValidationError("body exceeds 16 KiB")
    return value


def _failure_code(value: str) -> str:
    normalized = _required(value, "failure_code")
    if not _FAILURE_CODE_RE.fullmatch(normalized):
        raise ArtifactValidationError("failure_code must be a bounded machine-readable token")
    return normalized


def _lease_fields(lease: WriterLease | None) -> tuple[str | None, int | None]:
    if lease is None:
        return None, None
    return (
        _required(lease.lease_id, "lease_id"),
        _positive(lease.fencing_token, "fencing_token"),
    )


class ArtifactSessionService:
    """Stable orchestration surface above :class:`ArtifactSessionRepository`."""

    def __init__(self, repository: ArtifactSessionRepository) -> None:
        self.repository = repository

    @classmethod
    async def from_session_storage(
        cls,
        storage: _SessionStorageBinding,
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> ArtifactSessionService:
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        if id_factory is not None:
            kwargs["id_factory"] = id_factory
        repository = await ArtifactSessionRepository.from_session_storage(storage, **kwargs)
        return cls(repository)

    @classmethod
    async def open(
        cls,
        db_path: str | Path = ":memory:",
        *,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
    ) -> ArtifactSessionService:
        kwargs: dict[str, Any] = {}
        if clock is not None:
            kwargs["clock"] = clock
        if id_factory is not None:
            kwargs["id_factory"] = id_factory
        repository = await ArtifactSessionRepository.open(db_path, **kwargs)
        return cls(repository)

    async def close(self) -> None:
        await self.repository.close()

    def allocate_id(self, prefix: str) -> str:
        """Allocate an opaque id for a transaction-prepared internal record."""

        return self.repository.allocate_id(_required(prefix, "prefix"))

    async def __aenter__(self) -> ArtifactSessionService:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def create_document(
        self,
        *,
        session_key: str,
        name: str,
        kind: ArtifactKind,
        initial_artifact: ArtifactBlobRef,
        actor: Actor,
        session_id: str | None = None,
        document_id: str | None = None,
        revision_id: str | None = None,
    ) -> CommitResult:
        return await self.repository.create_document(
            session_key=_required(session_key, "session_key"),
            session_id=None if session_id is None else _required(session_id, "session_id"),
            name=_required(name, "name"),
            kind=kind,
            initial_artifact=_blob(initial_artifact),
            actor=_actor(actor),
            document_id=(None if document_id is None else _required(document_id, "document_id")),
            revision_id=(None if revision_id is None else _required(revision_id, "revision_id")),
        )

    async def get_document(self, document_id: str) -> Document:
        return await self.repository.get_document(_required(document_id, "document_id"))

    async def get_document_head(
        self,
        document_id: str,
        *,
        expected_revision_id: str | None = None,
    ) -> CommitResult:
        """Read and optionally fence the current head in one repository snapshot."""

        return await self.repository.get_document_head(
            _required(document_id, "document_id"),
            expected_revision_id=(
                None
                if expected_revision_id is None
                else _required(expected_revision_id, "expected_revision_id")
            ),
        )

    async def adopt_document(
        self,
        *,
        session_key: str,
        session_id: str,
        name: str,
        kind: ArtifactKind,
        initial_artifact: ArtifactBlobRef,
        actor: Actor,
    ) -> tuple[CommitResult, bool]:
        return await self.repository.adopt_document(
            session_key=_required(session_key, "session_key"),
            session_id=_required(session_id, "session_id"),
            name=_required(name, "name"),
            kind=kind,
            initial_artifact=_blob(initial_artifact),
            actor=_actor(actor),
        )

    async def adopt_generated_deliverable(
        self,
        *,
        session_key: str,
        session_id: str,
        name: str,
        kind: ArtifactKind,
        deliverable: ArtifactBlobRef,
        actor: Actor,
    ) -> tuple[CommitResult, DocumentSourceBinding, bool]:
        """Adopt one immutable generated deliverable as a stable Document.

        The initial revision references the already-published ArtifactStore
        object directly.  The repository creates the Document and its source
        binding in one transaction, so concurrent publication/open paths
        converge on one logical identity without copying the public bytes.
        """

        return await self.repository.adopt_generated_deliverable(
            session_key=_bounded_text(session_key, "session_key", max_bytes=2048),
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            name=_bounded_text(name, "name", max_bytes=512),
            kind=kind,
            deliverable=_blob(deliverable),
            actor=_actor(actor),
        )

    async def reserve_document_import_attempt(
        self,
        *,
        session_key: str,
        session_id: str,
        idempotency_key: str,
        source_type: DocumentSourceType,
        source_resource_id: str,
        source_sha256: str,
        source_name: str,
        source_mime: str,
        source_size: int,
        document_name: str,
        mode: DocumentImportMode,
        candidate_artifact_id: str,
        attempt_id: str | None = None,
    ) -> tuple[DocumentImportAttempt, bool]:
        if mode is not DocumentImportMode.COPY:
            raise ArtifactValidationError("only copy imports are supported")
        source = _blob(
            ArtifactBlobRef(
                artifact_id=candidate_artifact_id,
                sha256=source_sha256,
                filename=source_name,
                media_type=source_mime,
                byte_size=source_size,
            )
        )
        return await self.repository.reserve_document_import_attempt(
            session_key=_bounded_text(session_key, "session_key", max_bytes=2048),
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
            source_type=source_type,
            source_resource_id=_bounded_text(
                source_resource_id,
                "source_resource_id",
                max_bytes=512,
            ),
            source_sha256=source.sha256,
            source_name=source.filename,
            source_mime=source.media_type,
            source_size=source.byte_size,
            document_name=_bounded_text(
                document_name,
                "document_name",
                max_bytes=512,
            ),
            mode=mode,
            candidate_artifact_id=source.artifact_id,
            attempt_id=(
                None
                if attempt_id is None
                else _bounded_text(attempt_id, "attempt_id", max_bytes=256)
            ),
        )

    async def get_document_import_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentImportAttempt:
        return await self.repository.get_document_import_attempt(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
        )

    async def list_document_import_attempts_for_recovery(
        self,
        *,
        limit: int = 100,
        after_attempt_id: str | None = None,
    ) -> tuple[DocumentImportAttempt, ...]:
        return await self.repository.list_document_import_attempts_for_recovery(
            limit=_bounded_limit(limit),
            after_attempt_id=(
                None
                if after_attempt_id is None
                else _bounded_text(after_attempt_id, "after_attempt_id", max_bytes=256)
            ),
        )

    async def mark_document_import_candidate_cleaned(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentImportAttempt:
        return await self.repository.mark_document_import_candidate_cleaned(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
        )

    async def get_document_source_binding(
        self,
        binding_id: str,
    ) -> DocumentSourceBinding:
        return await self.repository.get_document_source_binding(
            _bounded_text(binding_id, "binding_id", max_bytes=256)
        )

    async def get_document_source_binding_for_resource(
        self,
        *,
        session_id: str,
        source_type: DocumentSourceType,
        source_resource_id: str,
    ) -> DocumentSourceBinding | None:
        return await self.repository.get_document_source_binding_for_resource(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            source_type=source_type,
            source_resource_id=_bounded_text(
                source_resource_id,
                "source_resource_id",
                max_bytes=512,
            ),
        )

    async def list_document_source_bindings(
        self,
        *,
        session_id: str,
        limit: int = 500,
    ) -> tuple[DocumentSourceBinding, ...]:
        return await self.repository.list_document_source_bindings(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            limit=_bounded_limit(limit),
        )

    async def apply_document_import_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        candidate_artifact: ArtifactBlobRef,
        document_name: str,
        kind: ArtifactKind,
        actor: Actor,
    ) -> DocumentImportResult:
        return await self.repository.apply_document_import_attempt(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
            candidate_artifact=_blob(candidate_artifact),
            document_name=_bounded_text(document_name, "document_name", max_bytes=512),
            kind=kind,
            actor=_actor(actor),
        )

    async def fail_document_import_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        failure_code: str,
        ambiguous: bool = False,
    ) -> DocumentImportAttempt:
        return await self.repository.fail_document_import_attempt(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
            failure_code=_failure_code(failure_code),
            ambiguous=bool(ambiguous),
        )

    async def reserve_document_publish_attempt(
        self,
        *,
        session_key: str,
        session_id: str,
        idempotency_key: str,
        document_id: str,
        revision_id: str,
        candidate_artifact: ArtifactBlobRef,
        attempt_id: str | None = None,
    ) -> tuple[DocumentPublishAttempt, bool]:
        return await self.repository.reserve_document_publish_attempt(
            session_key=_bounded_text(session_key, "session_key", max_bytes=2048),
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
            document_id=_bounded_text(document_id, "document_id", max_bytes=256),
            revision_id=_bounded_text(revision_id, "revision_id", max_bytes=256),
            candidate_artifact=_blob(candidate_artifact),
            attempt_id=(
                None
                if attempt_id is None
                else _bounded_text(attempt_id, "attempt_id", max_bytes=256)
            ),
        )

    async def get_document_publish_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentPublishAttempt:
        return await self.repository.get_document_publish_attempt(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
        )

    async def list_document_publish_attempts_for_recovery(
        self,
        *,
        limit: int = 100,
        after_attempt_id: str | None = None,
    ) -> tuple[DocumentPublishAttempt, ...]:
        return await self.repository.list_document_publish_attempts_for_recovery(
            limit=_bounded_limit(limit),
            after_attempt_id=(
                None
                if after_attempt_id is None
                else _bounded_text(after_attempt_id, "after_attempt_id", max_bytes=256)
            ),
        )

    async def mark_document_publish_promoted(
        self,
        *,
        session_id: str,
        idempotency_key: str,
    ) -> DocumentPublishAttempt:
        return await self.repository.mark_document_publish_promoted(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
        )

    async def apply_document_publish_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        actor: Actor,
    ) -> DocumentPublishResult:
        return await self.repository.apply_document_publish_attempt(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
            actor=_actor(actor),
        )

    async def fail_document_publish_attempt(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        failure_code: str,
        ambiguous: bool = False,
    ) -> DocumentPublishAttempt:
        return await self.repository.fail_document_publish_attempt(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            idempotency_key=_bounded_text(
                idempotency_key,
                "idempotency_key",
                max_bytes=256,
            ),
            failure_code=_failure_code(failure_code),
            ambiguous=bool(ambiguous),
        )

    async def get_document_publication(
        self,
        publication_id: str,
    ) -> DocumentPublication:
        return await self.repository.get_document_publication(
            _bounded_text(publication_id, "publication_id", max_bytes=256)
        )

    async def list_document_publications(
        self,
        *,
        session_id: str,
        document_id: str | None = None,
        limit: int = 500,
    ) -> tuple[DocumentPublication, ...]:
        return await self.repository.list_document_publications(
            session_id=_bounded_text(session_id, "session_id", max_bytes=512),
            document_id=(
                None
                if document_id is None
                else _bounded_text(document_id, "document_id", max_bytes=256)
            ),
            limit=_bounded_limit(limit),
        )

    async def rename_document(
        self,
        *,
        document_id: str,
        expected_state_revision: int,
        name: str,
        actor: Actor,
    ) -> Document:
        return await self.repository.rename_document(
            document_id=_required(document_id, "document_id"),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            name=_required(name, "name"),
            actor=_actor(actor),
        )

    async def list_documents(
        self,
        *,
        session_key: str,
        session_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Document, ...]:
        return await self.repository.list_documents(
            session_key=_required(session_key, "session_key"),
            session_id=(None if session_id is None else _required(session_id, "session_id")),
            limit=_bounded_limit(limit),
        )

    async def list_rejected_candidate_artifacts(
        self,
        *,
        limit: int = 500,
    ) -> tuple[tuple[str, str, str, str], ...]:
        """List physical candidate blobs still journaled by rejected drafts."""

        return await self.repository.list_rejected_candidate_artifacts(
            limit=_bounded_limit(limit),
        )

    async def list_applied_candidate_artifacts(
        self,
        *,
        limit: int = 500,
    ) -> tuple[tuple[str, str, str, str, str], ...]:
        """List superseded candidate blobs still journaled by applied turns."""

        return await self.repository.list_applied_candidate_artifacts(
            limit=_bounded_limit(limit),
        )

    async def mark_candidate_artifact_cleaned(
        self,
        *,
        document_id: str,
        artifact_id: str,
        sha256: str,
        actor: Actor,
    ) -> None:
        """Persist the idempotent completion marker for physical cleanup."""

        digest = _required(sha256, "sha256")
        if not _SHA256_RE.fullmatch(digest):
            raise ArtifactValidationError(
                "sha256 must contain exactly 64 hexadecimal characters"
            )
        await self.repository.mark_candidate_artifact_cleaned(
            document_id=_required(document_id, "document_id"),
            artifact_id=_required(artifact_id, "artifact_id"),
            sha256=digest.lower(),
            actor=_actor(actor),
        )

    async def snapshot_session_heads(self, *, session_id: str) -> tuple[CommitResult, ...]:
        return await self.repository.snapshot_session_heads(
            session_id=_required(session_id, "session_id"),
        )

    async def fork_session_heads(
        self,
        *,
        source_session_id: str,
        target_session_key: str,
        target_session_id: str,
        snapshots: Sequence[CommitResult],
        actor: Actor,
    ) -> tuple[CommitResult, ...]:
        return await self.repository.fork_session_heads(
            source_session_id=_required(source_session_id, "source_session_id"),
            target_session_key=_required(target_session_key, "target_session_key"),
            target_session_id=_required(target_session_id, "target_session_id"),
            snapshots=tuple(snapshots),
            actor=_actor(actor),
        )

    async def get_revision(self, revision_id: str) -> Revision:
        return await self.repository.get_revision(_required(revision_id, "revision_id"))

    async def list_revisions(
        self,
        document_id: str,
        *,
        limit: int = 100,
    ) -> tuple[Revision, ...]:
        return await self.repository.list_revisions(
            _required(document_id, "document_id"),
            limit=_bounded_limit(limit),
        )

    async def commit_revision(
        self,
        *,
        document_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        artifact: ArtifactBlobRef,
        actor: Actor,
        source: RevisionSource = RevisionSource.MANUAL,
        lease: WriterLease | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        lease_id, fencing_token = _lease_fields(lease)
        return await self.repository.commit_revision(
            document_id=_required(document_id, "document_id"),
            expected_head_revision_id=_required(
                expected_head_revision_id, "expected_head_revision_id"
            ),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            artifact=_blob(artifact),
            actor=_actor(actor),
            source=source,
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
        )

    async def restore_revision(
        self,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
        lease: WriterLease | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        lease_id, fencing_token = _lease_fields(lease)
        return await self.repository.restore_revision(
            document_id=_required(document_id, "document_id"),
            target_revision_id=_required(target_revision_id, "target_revision_id"),
            expected_head_revision_id=_required(
                expected_head_revision_id, "expected_head_revision_id"
            ),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            actor=_actor(actor),
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
        )

    async def revert_revision(
        self,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
        lease: WriterLease | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        lease_id, fencing_token = _lease_fields(lease)
        return await self.repository.revert_revision(
            document_id=_required(document_id, "document_id"),
            target_revision_id=_required(target_revision_id, "target_revision_id"),
            expected_head_revision_id=_required(
                expected_head_revision_id, "expected_head_revision_id"
            ),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            actor=_actor(actor),
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
        )

    async def acquire_writer_lease(
        self,
        *,
        document_id: str,
        holder_id: str,
        ttl_ms: int,
        actor: Actor,
    ) -> WriterLease:
        return await self.repository.acquire_writer_lease(
            document_id=_required(document_id, "document_id"),
            holder_id=_required(holder_id, "holder_id"),
            ttl_ms=_positive(ttl_ms, "ttl_ms"),
            actor=_actor(actor),
        )

    async def get_writer_lease(self, document_id: str) -> WriterLease | None:
        return await self.repository.get_writer_lease(_required(document_id, "document_id"))

    async def renew_writer_lease(
        self,
        *,
        lease: WriterLease,
        ttl_ms: int,
        actor: Actor,
    ) -> WriterLease:
        lease_id, fencing_token = _lease_fields(lease)
        assert lease_id is not None and fencing_token is not None
        return await self.repository.renew_writer_lease(
            document_id=_required(lease.document_id, "document_id"),
            lease_id=lease_id,
            fencing_token=fencing_token,
            ttl_ms=_positive(ttl_ms, "ttl_ms"),
            actor=_actor(actor),
        )

    async def release_writer_lease(self, *, lease: WriterLease, actor: Actor) -> None:
        lease_id, fencing_token = _lease_fields(lease)
        assert lease_id is not None and fencing_token is not None
        await self.repository.release_writer_lease(
            document_id=_required(lease.document_id, "document_id"),
            lease_id=lease_id,
            fencing_token=fencing_token,
            actor=_actor(actor),
        )

    async def create_change_set(
        self,
        *,
        document_id: str,
        base_revision_id: str,
        operations: Sequence[dict[str, Any]],
        actor: Actor,
        turn_id: str | None = None,
        summary: str = "",
        change_set_id: str | None = None,
        candidate_loop: bool = False,
    ) -> ChangeSet:
        if not operations:
            raise ArtifactValidationError("operations must not be empty")
        _json_value(list(operations), "operations")
        normalized_turn_id = None if turn_id is None else _required(turn_id, "turn_id")
        if not isinstance(summary, str):
            raise ArtifactValidationError("summary must be a string")
        if len(summary) > 4_000:
            raise ArtifactValidationError("summary is too long")
        if not isinstance(candidate_loop, bool):
            raise ArtifactValidationError("candidate_loop must be a boolean")
        return await self.repository.create_change_set(
            document_id=_required(document_id, "document_id"),
            base_revision_id=_required(base_revision_id, "base_revision_id"),
            operations=operations,
            actor=_actor(actor),
            turn_id=normalized_turn_id,
            summary=summary.strip(),
            change_set_id=(
                None if change_set_id is None else _required(change_set_id, "change_set_id")
            ),
            candidate_loop=candidate_loop,
        )

    async def get_change_set(self, change_set_id: str) -> ChangeSet:
        return await self.repository.get_change_set(_required(change_set_id, "change_set_id"))

    async def is_candidate_loop_change_set(self, change_set_id: str) -> bool:
        """Check the immutable candidate-loop creation marker."""

        return await self.repository.is_candidate_loop_change_set(
            _required(change_set_id, "change_set_id")
        )

    async def get_change_set_by_turn(
        self,
        *,
        document_id: str,
        turn_id: str,
    ) -> ChangeSet | None:
        return await self.repository.get_change_set_by_turn(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
        )

    async def list_change_sets(
        self,
        document_id: str,
        *,
        status: ChangeSetStatus | None = None,
        limit: int = 100,
    ) -> tuple[ChangeSet, ...]:
        return await self.repository.list_change_sets(
            _required(document_id, "document_id"),
            status=status,
            limit=_bounded_limit(limit),
        )

    async def ready_change_set(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        candidate_artifact: ArtifactBlobRef,
        actor: Actor,
        validation: dict[str, Any] | None = None,
    ) -> ChangeSet:
        if validation is not None:
            _json_value(validation, "validation")
        return await self.repository.ready_change_set(
            change_set_id=_required(change_set_id, "change_set_id"),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            candidate_artifact=_blob(candidate_artifact),
            validation=validation,
            actor=_actor(actor),
        )

    async def update_draft_change_set_candidate(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        candidate_artifact: ArtifactBlobRef,
        operations: Sequence[dict[str, Any]],
        actor: Actor,
        validation: dict[str, Any] | None = None,
    ) -> ChangeSet:
        """Stage a replacement candidate while keeping the change set draft."""

        if not operations:
            raise ArtifactValidationError("operations must not be empty")
        _json_value(list(operations), "operations")
        if validation is not None:
            _json_value(validation, "validation")
        return await self.repository.update_draft_change_set_candidate(
            change_set_id=_required(change_set_id, "change_set_id"),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            candidate_artifact=_blob(candidate_artifact),
            operations=operations,
            validation=validation,
            actor=_actor(actor),
        )

    async def reject_change_set(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        actor: Actor,
        reason: str | None = None,
    ) -> ChangeSet:
        return await self.repository.reject_change_set(
            change_set_id=_required(change_set_id, "change_set_id"),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            actor=_actor(actor),
            reason=reason,
        )

    async def reject_draft_change_set_and_cleanup(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        actor: Actor,
        reason: str | None = None,
        require_no_active_mutation_attempt: bool = False,
    ) -> ChangeSet:
        """Reject a draft and detach its transient candidate reference."""

        if not isinstance(require_no_active_mutation_attempt, bool):
            raise ArtifactValidationError(
                "require_no_active_mutation_attempt must be a boolean"
            )
        if reason is not None:
            if not isinstance(reason, str):
                raise ArtifactValidationError("reason must be a string")
            if len(reason) > 4_000:
                raise ArtifactValidationError("reason is too long")
        return await self.repository.reject_draft_change_set_and_cleanup(
            change_set_id=_required(change_set_id, "change_set_id"),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            actor=_actor(actor),
            reason=reason,
            require_no_active_mutation_attempt=require_no_active_mutation_attempt,
        )

    async def reject_candidate_draft_and_fail_attempt_for_recovery(
        self,
        *,
        change_set_id: str,
        expected_state_revision: int,
        actor: Actor,
        reason: str,
        failure_code: str,
    ) -> tuple[ChangeSet, MutationAttempt | None]:
        """Atomically close a restart-orphaned candidate and mutation receipt."""

        normalized_reason = _required(reason, "reason")
        if len(normalized_reason) > 4_000:
            raise ArtifactValidationError("reason is too long")
        normalized_failure_code = _required(failure_code, "failure_code")
        if not _FAILURE_CODE_RE.fullmatch(normalized_failure_code):
            raise ArtifactValidationError("failure_code has an invalid format")
        normalized_actor = _actor(actor)
        if (
            normalized_actor.kind is not ActorKind.SYSTEM
            or normalized_actor.actor_id != "restart-recovery"
        ):
            raise ArtifactValidationError(
                "candidate recovery rejection requires the restart-recovery actor"
            )
        return await self.repository.reject_candidate_draft_and_fail_attempt_for_recovery(
            change_set_id=_required(change_set_id, "change_set_id"),
            expected_state_revision=_positive(
                expected_state_revision,
                "expected_state_revision",
            ),
            actor=normalized_actor,
            reason=normalized_reason,
            failure_code=normalized_failure_code,
        )

    async def list_draft_change_sets(
        self,
        *,
        limit: int = 100,
        candidate_only: bool = False,
    ) -> tuple[ChangeSet, ...]:
        """List drafts, optionally limiting results to agent turn candidates."""

        if not isinstance(candidate_only, bool):
            raise ArtifactValidationError("candidate_only must be a boolean")
        return await self.repository.list_draft_change_sets(
            limit=_bounded_limit(limit),
            candidate_only=candidate_only,
        )

    async def list_applied_candidate_change_sets(
        self,
        *,
        limit: int = 100,
    ) -> tuple[tuple[str, str, str, str], ...]:
        """List applied agent turns whose superseded candidate blobs may remain."""

        return await self.repository.list_applied_candidate_change_sets(
            limit=_bounded_limit(limit),
        )

    async def apply_change_set(
        self,
        *,
        change_set_id: str,
        expected_change_set_state_revision: int,
        expected_head_revision_id: str,
        expected_document_state_revision: int,
        actor: Actor,
        lease: WriterLease | None = None,
        require_lease: bool = False,
    ) -> CommitResult:
        lease_id, fencing_token = _lease_fields(lease)
        return await self.repository.apply_change_set(
            change_set_id=_required(change_set_id, "change_set_id"),
            expected_change_set_state_revision=_positive(
                expected_change_set_state_revision,
                "expected_change_set_state_revision",
            ),
            expected_head_revision_id=_required(
                expected_head_revision_id, "expected_head_revision_id"
            ),
            expected_document_state_revision=_positive(
                expected_document_state_revision,
                "expected_document_state_revision",
            ),
            actor=_actor(actor),
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
        )

    async def commit_draft_change_set_atomically(
        self,
        *,
        change_set_id: str,
        expected_change_set_state_revision: int,
        expected_head_revision_id: str,
        expected_document_state_revision: int,
        actor: Actor,
        expected_candidate_sha256: str | None = None,
        source: RevisionSource = RevisionSource.AGENT,
        revision_event_type: str = "revision.change_set_applied",
        lease: WriterLease | None = None,
        require_lease: bool = False,
        mutation_attempt_id: str | None = None,
        mutation_attempt_tool_use_id: str | None = None,
    ) -> tuple[CommitResult, ChangeSet]:
        """Atomically publish a previously staged draft candidate."""

        candidate_sha = None
        if expected_candidate_sha256 is not None:
            candidate_sha = _required(expected_candidate_sha256, "expected_candidate_sha256")
            if not _SHA256_RE.fullmatch(candidate_sha):
                raise ArtifactValidationError(
                    "expected_candidate_sha256 must contain exactly 64 hexadecimal characters"
                )
        lease_id, fencing_token = _lease_fields(lease)
        return await self.repository.commit_draft_change_set_atomically(
            change_set_id=_required(change_set_id, "change_set_id"),
            expected_change_set_state_revision=_positive(
                expected_change_set_state_revision,
                "expected_change_set_state_revision",
            ),
            expected_head_revision_id=_required(
                expected_head_revision_id,
                "expected_head_revision_id",
            ),
            expected_document_state_revision=_positive(
                expected_document_state_revision,
                "expected_document_state_revision",
            ),
            actor=_actor(actor),
            expected_candidate_sha256=(
                None if candidate_sha is None else candidate_sha.lower()
            ),
            source=source,
            revision_event_type=_required(revision_event_type, "revision_event_type"),
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
            mutation_attempt_id=(
                None
                if mutation_attempt_id is None
                else _required(mutation_attempt_id, "mutation_attempt_id")
            ),
            mutation_attempt_tool_use_id=(
                None
                if mutation_attempt_tool_use_id is None
                else _required(
                    mutation_attempt_tool_use_id,
                    "mutation_attempt_tool_use_id",
                )
            ),
        )

    async def commit_change_set_atomically(
        self,
        *,
        document_id: str,
        base_revision_id: str,
        expected_document_state_revision: int,
        operations: Sequence[dict[str, Any]],
        candidate_artifact: ArtifactBlobRef,
        validation: dict[str, Any] | None,
        actor: Actor,
        turn_id: str,
        summary: str = "",
        change_set_id: str | None = None,
        source: RevisionSource = RevisionSource.AGENT,
        copied_from_revision_id: str | None = None,
        revision_event_type: str = "revision.change_set_applied",
        lease: WriterLease | None = None,
        require_lease: bool = False,
        edit_session_id: str | None = None,
        expected_edit_session_state_revision: int | None = None,
        expected_last_saved_revision_id: str | None = None,
    ) -> tuple[CommitResult, ChangeSet]:
        """Persist a change set and its head revision as one unit."""

        if not operations:
            raise ArtifactValidationError("operations must not be empty")
        _json_value(list(operations), "operations")
        if validation is not None:
            _json_value(validation, "validation")
        if not isinstance(summary, str):
            raise ArtifactValidationError("summary must be a string")
        if len(summary) > 4_000:
            raise ArtifactValidationError("summary is too long")
        lease_id, fencing_token = _lease_fields(lease)
        return await self.repository.commit_change_set_atomically(
            document_id=_required(document_id, "document_id"),
            base_revision_id=_required(base_revision_id, "base_revision_id"),
            expected_document_state_revision=_positive(
                expected_document_state_revision,
                "expected_document_state_revision",
            ),
            operations=operations,
            candidate_artifact=_blob(candidate_artifact),
            validation=validation,
            actor=_actor(actor),
            turn_id=_required(turn_id, "turn_id"),
            summary=summary.strip(),
            change_set_id=(
                None if change_set_id is None else _required(change_set_id, "change_set_id")
            ),
            source=source,
            copied_from_revision_id=(
                None
                if copied_from_revision_id is None
                else _required(copied_from_revision_id, "copied_from_revision_id")
            ),
            revision_event_type=_required(revision_event_type, "revision_event_type"),
            lease_id=lease_id,
            fencing_token=fencing_token,
            require_lease=require_lease,
            edit_session_id=(
                None if edit_session_id is None else _required(edit_session_id, "edit_session_id")
            ),
            expected_edit_session_state_revision=(
                None
                if expected_edit_session_state_revision is None
                else _positive(
                    expected_edit_session_state_revision,
                    "expected_edit_session_state_revision",
                )
            ),
            expected_last_saved_revision_id=(
                None
                if expected_last_saved_revision_id is None
                else _required(
                    expected_last_saved_revision_id,
                    "expected_last_saved_revision_id",
                )
            ),
        )

    async def reserve_mutation_attempt(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        base_revision_id: str,
        proposal_sha256: str | None,
        mutation_attempt_id: str | None = None,
        candidate_change_set_id: str | None = None,
        expected_candidate_state_revision: int | None = None,
    ) -> MutationAttempt:
        attempt, _created = await self.reserve_mutation_attempt_with_status(
            document_id=document_id,
            turn_id=turn_id,
            tool_use_id=tool_use_id,
            base_revision_id=base_revision_id,
            proposal_sha256=proposal_sha256,
            mutation_attempt_id=mutation_attempt_id,
            candidate_change_set_id=candidate_change_set_id,
            expected_candidate_state_revision=expected_candidate_state_revision,
        )
        return attempt

    async def reserve_mutation_attempt_with_status(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        base_revision_id: str,
        proposal_sha256: str | None,
        mutation_attempt_id: str | None = None,
        candidate_change_set_id: str | None = None,
        expected_candidate_state_revision: int | None = None,
    ) -> tuple[MutationAttempt, bool]:
        proposal_sha = None if proposal_sha256 is None else proposal_sha256.strip()
        if proposal_sha is not None and not _SHA256_RE.fullmatch(proposal_sha):
            raise ArtifactValidationError(
                "proposal_sha256 must contain exactly 64 hexadecimal characters"
            )
        return await self.repository.reserve_mutation_attempt_with_status(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
            tool_use_id=_required(tool_use_id, "tool_use_id"),
            base_revision_id=_required(base_revision_id, "base_revision_id"),
            proposal_sha256=(None if proposal_sha is None else proposal_sha.lower()),
            mutation_attempt_id=(
                None
                if mutation_attempt_id is None
                else _required(mutation_attempt_id, "mutation_attempt_id")
            ),
            candidate_change_set_id=(
                None
                if candidate_change_set_id is None
                else _required(candidate_change_set_id, "candidate_change_set_id")
            ),
            expected_candidate_state_revision=(
                None
                if expected_candidate_state_revision is None
                else _positive(
                    expected_candidate_state_revision,
                    "expected_candidate_state_revision",
                )
            ),
        )

    async def reconcile_mutation_attempt(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
    ) -> MutationAttempt:
        return await self.repository.reconcile_mutation_attempt(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
            tool_use_id=_required(tool_use_id, "tool_use_id"),
        )

    async def get_mutation_attempt_for_resolution(
        self,
        *,
        document_id: str,
        turn_id: str,
    ) -> MutationAttempt:
        """Return a durable receipt for a trusted, session-scoped outcome query."""

        return await self.repository.get_mutation_attempt_for_resolution(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
        )

    async def register_mutation_candidate(
        self,
        *,
        document_id: str,
        turn_id: str,
        candidate_session_id: str,
        candidate_artifact_id: str,
        candidate_artifact_sha256: str,
    ) -> MutationAttempt:
        candidate_sha = _required(candidate_artifact_sha256, "candidate_artifact_sha256")
        if not _SHA256_RE.fullmatch(candidate_sha):
            raise ArtifactValidationError(
                "candidate_artifact_sha256 must contain exactly 64 hexadecimal characters"
            )
        return await self.repository.register_mutation_candidate(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
            candidate_session_id=_required(candidate_session_id, "candidate_session_id"),
            candidate_artifact_id=_required(candidate_artifact_id, "candidate_artifact_id"),
            candidate_artifact_sha256=candidate_sha.lower(),
        )

    async def list_unresolved_mutation_attempts(
        self,
        *,
        limit: int = 100,
        after_mutation_attempt_id: str | None = None,
    ) -> tuple[MutationAttempt, ...]:
        return await self.repository.list_unresolved_mutation_attempts(
            limit=_bounded_limit(limit),
            after_mutation_attempt_id=(
                None
                if after_mutation_attempt_id is None
                else _required(after_mutation_attempt_id, "after_mutation_attempt_id")
            ),
        )

    async def list_mutation_attempts_by_turn_ids(
        self,
        *,
        session_key: str,
        turn_ids: Sequence[str],
    ) -> tuple[MutationAttempt, ...]:
        """Load a bounded exact receipt set scoped to one canonical session."""

        normalized_turn_ids = tuple(
            dict.fromkeys(_required(turn_id, "turn_id") for turn_id in turn_ids)
        )
        if len(normalized_turn_ids) > 1000:
            raise ArtifactValidationError("turn_ids may contain at most 1000 items")
        return await self.repository.list_mutation_attempts_by_turn_ids(
            session_key=_required(session_key, "session_key"),
            turn_ids=normalized_turn_ids,
        )

    async def mark_mutation_attempt_applied(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        change_set_id: str,
        revision_id: str,
    ) -> MutationAttempt:
        return await self.repository.mark_mutation_attempt_applied(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
            tool_use_id=_required(tool_use_id, "tool_use_id"),
            change_set_id=_required(change_set_id, "change_set_id"),
            revision_id=_required(revision_id, "revision_id"),
        )

    async def mark_mutation_attempt_failed(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        failure_code: str,
        change_set_id: str | None = None,
    ) -> MutationAttempt:
        return await self.repository.mark_mutation_attempt_failed(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
            tool_use_id=_required(tool_use_id, "tool_use_id"),
            failure_code=_failure_code(failure_code),
            change_set_id=(
                None if change_set_id is None else _required(change_set_id, "change_set_id")
            ),
        )

    async def mark_mutation_attempt_ambiguous(
        self,
        *,
        document_id: str,
        turn_id: str,
        tool_use_id: str,
        failure_code: str,
        change_set_id: str | None = None,
        revision_id: str | None = None,
    ) -> MutationAttempt:
        return await self.repository.mark_mutation_attempt_ambiguous(
            document_id=_required(document_id, "document_id"),
            turn_id=_required(turn_id, "turn_id"),
            tool_use_id=_required(tool_use_id, "tool_use_id"),
            failure_code=_failure_code(failure_code),
            change_set_id=(
                None if change_set_id is None else _required(change_set_id, "change_set_id")
            ),
            revision_id=(None if revision_id is None else _required(revision_id, "revision_id")),
        )

    async def create_anchor(
        self,
        *,
        document_id: str,
        revision_id: str,
        kind: AnchorKind,
        locator: dict[str, Any],
        actor: Actor,
        quote: str | None = None,
        context: dict[str, Any] | None = None,
        state: AnchorState = AnchorState.RESOLVED,
        remapped_from_anchor_id: str | None = None,
        anchor_id: str | None = None,
    ) -> Anchor:
        if not locator:
            raise ArtifactValidationError("locator must not be empty")
        _json_value(locator, "locator")
        if context is not None:
            _json_value(context, "context")
        return await self.repository.create_anchor(
            document_id=_required(document_id, "document_id"),
            revision_id=_required(revision_id, "revision_id"),
            kind=kind,
            locator=locator,
            actor=_actor(actor),
            quote=quote,
            context=context,
            state=state,
            remapped_from_anchor_id=(
                None
                if remapped_from_anchor_id is None
                else _required(remapped_from_anchor_id, "remapped_from_anchor_id")
            ),
            anchor_id=None if anchor_id is None else _required(anchor_id, "anchor_id"),
        )

    async def get_anchor(self, anchor_id: str) -> Anchor:
        return await self.repository.get_anchor(_required(anchor_id, "anchor_id"))

    async def create_prompt_annotation_with_anchor(
        self,
        *,
        annotation_id: str,
        session_key: str,
        session_id: str,
        session_epoch: int,
        document_id: str,
        revision_id: str,
        kind: AnchorKind,
        locator: dict[str, Any],
        actor: Actor,
        quote: str | None = None,
        context: dict[str, Any] | None = None,
        body: str = "",
    ) -> tuple[Anchor, PromptAnnotation]:
        """Atomically create a source anchor and its prompt-annotation draft."""

        if not locator:
            raise ArtifactValidationError("locator must not be empty")
        _json_value(locator, "locator")
        if context is not None:
            _json_value(context, "context")
        return await self.repository.create_prompt_annotation_with_anchor(
            annotation_id=_required(annotation_id, "annotation_id"),
            session_key=_required(session_key, "session_key"),
            session_id=_required(session_id, "session_id"),
            session_epoch=_nonnegative(session_epoch, "session_epoch"),
            document_id=_required(document_id, "document_id"),
            revision_id=_required(revision_id, "revision_id"),
            kind=kind,
            locator=locator,
            actor=_actor(actor),
            quote=quote,
            context=context,
            body=_prompt_annotation_body(body),
        )

    async def create_prompt_annotation(
        self,
        *,
        annotation_id: str,
        session_key: str,
        session_id: str,
        session_epoch: int,
        document_id: str,
        revision_id: str,
        anchor_id: str,
        body: str = "",
    ) -> PromptAnnotation:
        return await self.repository.create_prompt_annotation(
            annotation_id=_required(annotation_id, "annotation_id"),
            session_key=_required(session_key, "session_key"),
            session_id=_required(session_id, "session_id"),
            session_epoch=_nonnegative(session_epoch, "session_epoch"),
            document_id=_required(document_id, "document_id"),
            revision_id=_required(revision_id, "revision_id"),
            anchor_id=_required(anchor_id, "anchor_id"),
            body=_prompt_annotation_body(body),
        )

    async def get_prompt_annotation(self, annotation_id: str) -> PromptAnnotation:
        return await self.repository.get_prompt_annotation(
            _required(annotation_id, "annotation_id")
        )

    async def list_prompt_annotations(
        self,
        *,
        session_key: str,
        session_id: str,
        session_epoch: int,
        status: PromptAnnotationStatus | None = None,
        document_id: str | None = None,
        limit: int = 500,
    ) -> tuple[PromptAnnotation, ...]:
        return await self.repository.list_prompt_annotations(
            session_key=_required(session_key, "session_key"),
            session_id=_required(session_id, "session_id"),
            session_epoch=_nonnegative(session_epoch, "session_epoch"),
            status=status,
            document_id=(None if document_id is None else _required(document_id, "document_id")),
            limit=_bounded_limit(limit),
        )

    async def update_prompt_annotation(
        self,
        *,
        annotation_id: str,
        expected_state_revision: int,
        body: str,
    ) -> PromptAnnotation:
        return await self.repository.update_prompt_annotation(
            annotation_id=_required(annotation_id, "annotation_id"),
            expected_state_revision=_positive(
                expected_state_revision,
                "expected_state_revision",
            ),
            body=_prompt_annotation_body(body),
        )

    async def discard_prompt_annotation(
        self,
        *,
        annotation_id: str,
        expected_state_revision: int,
    ) -> PromptAnnotation:
        return await self.repository.discard_prompt_annotation(
            annotation_id=_required(annotation_id, "annotation_id"),
            expected_state_revision=_positive(
                expected_state_revision,
                "expected_state_revision",
            ),
        )

    async def preflight_prompt_annotations(
        self,
        *,
        annotation_ids: Sequence[str],
        session_key: str,
        session_id: str,
        session_epoch: int,
        require_current_head: bool = True,
    ) -> tuple[PromptAnnotation, ...]:
        normalized_ids = tuple(_required(item, "annotation_id") for item in annotation_ids)
        return await self.repository.preflight_prompt_annotations(
            annotation_ids=normalized_ids,
            session_key=_required(session_key, "session_key"),
            session_id=_required(session_id, "session_id"),
            session_epoch=_nonnegative(session_epoch, "session_epoch"),
            require_current_head=require_current_head,
        )

    async def get_edit_session(self, edit_session_id: str) -> EditSession:
        return await self.repository.get_edit_session(_required(edit_session_id, "edit_session_id"))

    async def start_edit_session(
        self,
        *,
        document_id: str,
        user_id: str,
        ttl_ms: int,
        actor: Actor,
        edit_session_id: str,
    ) -> EditSession:
        return await self.repository.start_edit_session(
            document_id=_required(document_id, "document_id"),
            user_id=_required(user_id, "user_id"),
            ttl_ms=_positive(ttl_ms, "ttl_ms"),
            actor=_actor(actor),
            edit_session_id=_required(edit_session_id, "edit_session_id"),
        )

    async def validate_edit_session_for_save(
        self,
        *,
        edit_session_id: str,
        document_id: str,
        user_id: str,
        expected_state_revision: int,
        expected_last_saved_revision_id: str,
    ) -> EditSession:
        return await self.repository.validate_edit_session_for_save(
            edit_session_id=_required(edit_session_id, "edit_session_id"),
            document_id=_required(document_id, "document_id"),
            user_id=_required(user_id, "user_id"),
            expected_state_revision=_positive(
                expected_state_revision,
                "expected_state_revision",
            ),
            expected_last_saved_revision_id=_required(
                expected_last_saved_revision_id,
                "expected_last_saved_revision_id",
            ),
        )

    async def heartbeat_edit_session(
        self,
        *,
        edit_session_id: str,
        user_id: str,
        expected_state_revision: int,
        ttl_ms: int,
        actor: Actor,
    ) -> EditSession:
        return await self.repository.heartbeat_edit_session(
            edit_session_id=_required(edit_session_id, "edit_session_id"),
            user_id=_required(user_id, "user_id"),
            expected_state_revision=_positive(
                expected_state_revision,
                "expected_state_revision",
            ),
            ttl_ms=_positive(ttl_ms, "ttl_ms"),
            actor=_actor(actor),
        )

    async def close_edit_session(
        self,
        *,
        edit_session_id: str,
        user_id: str,
        expected_state_revision: int,
        actor: Actor,
    ) -> EditSession:
        return await self.repository.close_edit_session(
            edit_session_id=_required(edit_session_id, "edit_session_id"),
            user_id=_required(user_id, "user_id"),
            expected_state_revision=_positive(
                expected_state_revision,
                "expected_state_revision",
            ),
            actor=_actor(actor),
        )

    async def list_audit_events(
        self,
        document_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> tuple[AuditEvent, ...]:
        return await self.repository.list_audit_events(
            _required(document_id, "document_id"),
            after_sequence=_nonnegative(after_sequence, "after_sequence"),
            limit=_bounded_limit(limit),
        )

    async def latest_audit_event(self, document_id: str) -> AuditEvent | None:
        return await self.repository.latest_audit_event(_required(document_id, "document_id"))

    async def audit_event_for_mutation(
        self,
        document_id: str,
        *,
        revision_id: str | None = None,
        change_set_id: str | None = None,
    ) -> AuditEvent | None:
        """Find the durable audit sequence for one exact mutation.

        This is intentionally narrower than :meth:`latest_audit_event`: a
        transient ``source.patched`` notification may be replayed after a
        crash, and using the document's newest unrelated row would make the
        UI sequence fence ambiguous.
        """

        document = _required(document_id, "document_id")
        if revision_id is None and change_set_id is None:
            raise ArtifactValidationError(
                "revision_id or change_set_id is required for an exact audit lookup"
            )
        return await self.repository.audit_event_for_mutation(
            document,
            revision_id=(None if revision_id is None else _required(revision_id, "revision_id")),
            change_set_id=(
                None if change_set_id is None else _required(change_set_id, "change_set_id")
            ),
        )
