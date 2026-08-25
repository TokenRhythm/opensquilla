"""Typed records exchanged by the ArtifactSession repository and service."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ArtifactKind(StrEnum):
    """Logical editor family for a document."""

    DOCUMENT = "document"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    HTML = "html"
    OTHER = "other"


class ActorKind(StrEnum):
    """Identity class responsible for a durable mutation."""

    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class RevisionSource(StrEnum):
    """How a new immutable revision was produced."""

    INITIAL = "initial"
    MANUAL = "manual"
    AGENT = "agent"
    RESTORE = "restore"
    REVERT = "revert"


class ChangeSetStatus(StrEnum):
    """Lifecycle state for one atomic artifact change set."""

    DRAFT = "draft"
    READY = "ready"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICT = "conflict"
    FAILED = "failed"


class AnchorKind(StrEnum):
    """Stable locator family used by prompt annotations and change sets."""

    TEXT_RANGE = "text_range"
    CELL_RANGE = "cell_range"
    SLIDE_SHAPE = "slide_shape"
    DOM_SOURCE = "dom_source"
    GENERIC = "generic"


class AnchorState(StrEnum):
    """Whether an anchor still maps to its intended artifact content."""

    RESOLVED = "resolved"
    ORPHANED = "orphaned"


class PromptAnnotationStatus(StrEnum):
    """Lifecycle for an instruction attached to a future chat turn."""

    DRAFT = "draft"
    SENT = "sent"
    DISCARDED = "discarded"


class MutationAttemptStatus(StrEnum):
    """Crash-recovery state for one artifact-writing tool call."""

    RESERVED = "reserved"
    APPLIED = "applied"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class DocumentSourceType(StrEnum):
    """Immutable resource kind copied into a logical document."""

    ATTACHMENT = "attachment"
    DELIVERABLE = "deliverable"


class DocumentImportMode(StrEnum):
    """Supported source synchronization policy for imported documents."""

    COPY = "copy"


class EditSessionMode(StrEnum):
    """Effective access level for an editor session."""

    EDIT = "edit"


class EditSessionStatus(StrEnum):
    """Lifecycle state for an editor session."""

    ACTIVE = "active"
    CLOSED = "closed"
    EXPIRED = "expired"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class Actor:
    """Auditable caller identity."""

    kind: ActorKind
    actor_id: str


@dataclass(frozen=True, slots=True)
class ArtifactBlobRef:
    """Reference to immutable bytes managed by an external artifact store."""

    artifact_id: str
    sha256: str
    filename: str
    media_type: str
    byte_size: int


@dataclass(frozen=True, slots=True)
class Document:
    """Mutable head pointer and concurrency state for one logical artifact."""

    document_id: str
    session_key: str
    session_id: str | None
    name: str
    kind: ArtifactKind
    head_revision_id: str
    generation: int
    state_revision: int
    writer_fencing_token: int
    created_at: int
    updated_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class Revision:
    """Immutable snapshot reference in a document's linear history."""

    revision_id: str
    document_id: str
    parent_revision_id: str | None
    generation: int
    artifact_id: str
    artifact_sha256: str
    filename: str
    media_type: str
    byte_size: int
    source: RevisionSource
    actor_kind: ActorKind
    actor_id: str
    change_set_id: str | None
    copied_from_revision_id: str | None
    created_at: int
    schema_version: int = 1

    @property
    def artifact(self) -> ArtifactBlobRef:
        return ArtifactBlobRef(
            artifact_id=self.artifact_id,
            sha256=self.artifact_sha256,
            filename=self.filename,
            media_type=self.media_type,
            byte_size=self.byte_size,
        )


@dataclass(frozen=True, slots=True)
class ChangeSet:
    """Durable atomic change set based on a specific immutable revision."""

    change_set_id: str
    document_id: str
    base_revision_id: str
    turn_id: str | None
    summary: str
    status: ChangeSetStatus
    operations: tuple[dict[str, Any], ...]
    candidate_artifact_id: str | None
    candidate_artifact_sha256: str | None
    candidate_filename: str | None
    candidate_media_type: str | None
    candidate_byte_size: int | None
    validation: dict[str, Any] | None
    state_revision: int
    created_by_kind: ActorKind
    created_by_id: str
    applied_revision_id: str | None
    created_at: int
    updated_at: int
    schema_version: int = 1

    @property
    def candidate_artifact(self) -> ArtifactBlobRef | None:
        artifact_id = self.candidate_artifact_id
        sha256 = self.candidate_artifact_sha256
        filename = self.candidate_filename
        media_type = self.candidate_media_type
        byte_size = self.candidate_byte_size
        if (
            artifact_id is None
            or sha256 is None
            or filename is None
            or media_type is None
            or byte_size is None
        ):
            return None
        return ArtifactBlobRef(
            artifact_id=artifact_id,
            sha256=sha256,
            filename=filename,
            media_type=media_type,
            byte_size=byte_size,
        )


@dataclass(frozen=True, slots=True)
class Anchor:
    """Revision-scoped structured locator for an annotation or change set."""

    anchor_id: str
    document_id: str
    revision_id: str
    kind: AnchorKind
    locator: dict[str, Any]
    quote: str | None
    context: dict[str, Any] | None
    state: AnchorState
    remapped_from_anchor_id: str | None
    created_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class PromptAnnotation:
    """Durable prompt instruction bound to one exact artifact revision anchor."""

    annotation_id: str
    session_key: str
    session_id: str
    session_epoch: int
    document_id: str
    revision_id: str
    anchor_id: str
    body: str
    status: PromptAnnotationStatus
    state_revision: int
    sent_message_id: str | None
    sent_turn_id: str | None
    sent_order: int | None
    created_at: int
    updated_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class PreparedPromptAnnotationTarget:
    """Send-time anchor replacement fenced by one immutable draft snapshot.

    HTML parsing happens before turn acceptance.  This bounded value is then
    applied inside SessionStorage's acceptance transaction, where the document
    head and draft CAS are checked again before any transcript, task, or
    request receipt becomes visible.
    """

    expected_annotation: PromptAnnotation
    previous_anchor_id: str
    anchor_id: str
    audit_event_id: str
    revision_id: str
    kind: AnchorKind
    locator: dict[str, Any]
    quote: str | None
    context: dict[str, Any]
    state: AnchorState
    actor_kind: ActorKind
    actor_id: str


@dataclass(frozen=True, slots=True)
class MutationAttempt:
    """Durable idempotency receipt for one document mutation in one turn."""

    mutation_attempt_id: str
    document_id: str
    turn_id: str
    tool_use_id: str
    base_revision_id: str
    proposal_sha256: str | None
    status: MutationAttemptStatus
    change_set_id: str | None
    revision_id: str | None
    failure_code: str | None
    state_revision: int
    created_at: int
    updated_at: int
    schema_version: int = 1
    candidate_session_id: str | None = None
    candidate_artifact_id: str | None = None
    candidate_artifact_sha256: str | None = None
    candidate_registered_at: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentSourceBinding:
    """Immutable provenance link from a document to one source occurrence."""

    binding_id: str
    document_id: str
    session_key: str
    session_id: str
    source_type: DocumentSourceType
    source_resource_id: str
    source_sha256: str
    source_name: str
    source_mime: str
    source_size: int
    mode: DocumentImportMode
    created_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class DocumentImportAttempt:
    """Crash-recoverable, idempotent journal for one source import."""

    attempt_id: str
    session_key: str
    session_id: str
    idempotency_key: str
    source_type: DocumentSourceType
    source_resource_id: str
    source_sha256: str
    source_name: str
    source_mime: str
    source_size: int
    document_name: str
    mode: DocumentImportMode
    candidate_artifact_id: str
    status: MutationAttemptStatus
    document_id: str | None
    revision_id: str | None
    binding_id: str | None
    failure_code: str | None
    candidate_cleaned_at: int | None
    state_revision: int
    created_at: int
    updated_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class DocumentPublication:
    """Immutable delivery relation pinned to one exact document revision."""

    publication_id: str
    session_key: str
    session_id: str
    document_id: str
    revision_id: str
    deliverable_artifact_id: str
    artifact_sha256: str
    name: str
    mime: str
    size: int
    created_by_kind: ActorKind
    created_by_id: str
    created_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class DocumentPublishAttempt:
    """Crash-recoverable journal for publishing an immutable revision copy."""

    attempt_id: str
    session_key: str
    session_id: str
    idempotency_key: str
    document_id: str
    revision_id: str
    candidate_artifact_id: str
    artifact_sha256: str
    name: str
    mime: str
    size: int
    status: MutationAttemptStatus
    publication_id: str | None
    deliverable_artifact_id: str | None
    failure_code: str | None
    promoted_at: int | None
    state_revision: int
    created_at: int
    updated_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class DocumentImportResult:
    """Atomic import receipt returned by the repository."""

    attempt: DocumentImportAttempt
    binding: DocumentSourceBinding
    commit: CommitResult


@dataclass(frozen=True, slots=True)
class DocumentPublishResult:
    """Atomic immutable-publication receipt returned by the repository."""

    attempt: DocumentPublishAttempt
    publication: DocumentPublication


@dataclass(frozen=True, slots=True)
class EditSession:
    """Short-lived editor access bound to a document revision."""

    edit_session_id: str
    document_id: str
    base_revision_id: str
    last_saved_revision_id: str
    mode: EditSessionMode
    status: EditSessionStatus
    user_id: str
    state_revision: int
    expires_at: int
    last_access_at: int
    created_at: int
    updated_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class WriterLease:
    """Exclusive writer ownership carrying a monotonically increasing fence."""

    lease_id: str
    document_id: str
    holder_id: str
    fencing_token: int
    expires_at: int
    created_at: int
    updated_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """Append-only record of a durable ArtifactSession mutation."""

    sequence: int
    event_id: str
    document_id: str
    event_type: str
    actor_kind: ActorKind
    actor_id: str
    revision_id: str | None
    change_set_id: str | None
    anchor_id: str | None
    edit_session_id: str | None
    lease_id: str | None
    payload: dict[str, Any]
    created_at: int
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Atomic result of advancing a document head."""

    document: Document
    revision: Revision
