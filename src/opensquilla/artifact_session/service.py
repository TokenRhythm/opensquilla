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


def _failure_code(value: str) -> str:
    normalized = _required(value, "failure_code")
    if not _FAILURE_CODE_RE.fullmatch(normalized):
        raise ArtifactValidationError("failure_code must be a bounded machine-readable token")
    return normalized


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

    async def retire_legacy_html_state(self) -> None:
        """Retire discontinued editor state without deleting revisions or candidate bytes."""
        await self.repository.retire_legacy_html_state()

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
        working_source: dict[str, str] | None = None,
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
            working_source=working_source,
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
    ) -> CommitResult:
        return await self.repository.commit_revision(
            document_id=_required(document_id, "document_id"),
            expected_head_revision_id=_required(
                expected_head_revision_id, "expected_head_revision_id"
            ),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            artifact=_blob(artifact),
            actor=_actor(actor),
            source=source,
        )

    async def restore_revision(
        self,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
        turn_id: str | None = None,
        no_op: bool | None = None,
    ) -> CommitResult:
        return await self.repository.restore_revision(
            document_id=_required(document_id, "document_id"),
            target_revision_id=_required(target_revision_id, "target_revision_id"),
            expected_head_revision_id=_required(
                expected_head_revision_id, "expected_head_revision_id"
            ),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            actor=_actor(actor),
            turn_id=None if turn_id is None else _required(turn_id, "turn_id"),
            no_op=no_op,
        )

    async def revert_revision(
        self,
        *,
        document_id: str,
        target_revision_id: str,
        expected_head_revision_id: str,
        expected_state_revision: int,
        actor: Actor,
    ) -> CommitResult:
        return await self.repository.revert_revision(
            document_id=_required(document_id, "document_id"),
            target_revision_id=_required(target_revision_id, "target_revision_id"),
            expected_head_revision_id=_required(
                expected_head_revision_id, "expected_head_revision_id"
            ),
            expected_state_revision=_positive(expected_state_revision, "expected_state_revision"),
            actor=_actor(actor),
        )

    async def get_writer_lease(self, document_id: str) -> WriterLease | None:
        return await self.repository.get_writer_lease(_required(document_id, "document_id"))

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
    ) -> ChangeSet:
        if not operations:
            raise ArtifactValidationError("operations must not be empty")
        _json_value(list(operations), "operations")
        normalized_turn_id = None if turn_id is None else _required(turn_id, "turn_id")
        if not isinstance(summary, str):
            raise ArtifactValidationError("summary must be a string")
        if len(summary) > 4_000:
            raise ArtifactValidationError("summary is too long")
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
        )

    async def get_change_set(self, change_set_id: str) -> ChangeSet:
        return await self.repository.get_change_set(_required(change_set_id, "change_set_id"))

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

    async def list_draft_change_sets(self, *, limit: int = 100) -> tuple[ChangeSet, ...]:
        return await self.repository.list_draft_change_sets(limit=_bounded_limit(limit))

    async def apply_change_set(
        self,
        *,
        change_set_id: str,
        expected_change_set_state_revision: int,
        expected_head_revision_id: str,
        expected_document_state_revision: int,
        actor: Actor,
    ) -> CommitResult:
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

    async def get_edit_session(self, edit_session_id: str) -> EditSession:
        return await self.repository.get_edit_session(_required(edit_session_id, "edit_session_id"))

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
