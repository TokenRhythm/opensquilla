"""Transport-neutral Artifact Workbench use cases and Ports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


def _identity(value: str, label: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


@dataclass(frozen=True, slots=True)
class ArtifactCatalogQuery:
    session_key: str
    limit: int = 200
    before: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_key", _identity(self.session_key, "session key"))
        if self.limit < 1 or self.limit > 200:
            raise ValueError("artifact page limit must be between 1 and 200")


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    session_key: str
    artifact_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_key", _identity(self.session_key, "session key"))
        object.__setattr__(self, "artifact_id", _identity(self.artifact_id, "artifact id"))


@dataclass(frozen=True, slots=True)
class SessionDocumentsQuery:
    session_key: str
    limit: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_key", _identity(self.session_key, "session key"))
        if self.limit < 1:
            raise ValueError("document page limit must be positive")


@dataclass(frozen=True, slots=True)
class DocumentIdentity:
    session_key: str
    document_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_key", _identity(self.session_key, "session key"))
        object.__setattr__(self, "document_id", _identity(self.document_id, "document id"))


@dataclass(frozen=True, slots=True)
class DocumentCapabilitiesQuery:
    session_key: str | None = None
    document_id: str | None = None

    def __post_init__(self) -> None:
        if (self.session_key is None) != (self.document_id is None):
            raise ValueError("session key and document id must be supplied together")
        if self.session_key is not None:
            object.__setattr__(
                self, "session_key", _identity(self.session_key, "session key")
            )
            object.__setattr__(
                self, "document_id", _identity(self.document_id or "", "document id")
            )


@dataclass(frozen=True, slots=True)
class DocumentOpen:
    session_key: str
    artifact_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_key", _identity(self.session_key, "session key"))
        object.__setattr__(self, "artifact_id", _identity(self.artifact_id, "artifact id"))


@dataclass(frozen=True, slots=True)
class DocumentRename:
    session_key: str
    document_id: str
    expected_state_revision: int
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "session_key", _identity(self.session_key, "session key"))
        object.__setattr__(self, "document_id", _identity(self.document_id, "document id"))
        object.__setattr__(self, "name", _identity(self.name, "document name"))
        if self.expected_state_revision < 1:
            raise ValueError("expected state revision must be positive")


@dataclass(frozen=True, slots=True)
class RevisionListQuery:
    session_key: str
    document_id: str
    limit: int = 100

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)
        if self.limit < 1:
            raise ValueError("revision page limit must be positive")


@dataclass(frozen=True, slots=True)
class RevisionRestore:
    session_key: str
    document_id: str
    revision_id: str
    expected_head_revision_id: str
    expected_state_revision: int
    request_id: str

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)
        for value, label in (
            (self.revision_id, "revision id"),
            (self.expected_head_revision_id, "expected head revision id"),
            (self.request_id, "request id"),
        ):
            _identity(value, label)
        if self.expected_state_revision < 1:
            raise ValueError("expected state revision must be positive")


@dataclass(frozen=True, slots=True)
class ChangeListQuery:
    session_key: str
    document_id: str
    limit: int = 100

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)
        if self.limit < 1:
            raise ValueError("change page limit must be positive")


@dataclass(frozen=True, slots=True)
class ChangeIdentity:
    session_key: str
    document_id: str
    change_set_id: str

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)
        _identity(self.change_set_id, "change set id")


@dataclass(frozen=True, slots=True)
class ChangeRevert:
    session_key: str
    document_id: str
    change_set_id: str
    expected_head_revision_id: str
    expected_state_revision: int
    request_id: str

    def __post_init__(self) -> None:
        ChangeIdentity(self.session_key, self.document_id, self.change_set_id)
        _identity(self.expected_head_revision_id, "expected head revision id")
        _identity(self.request_id, "request id")
        if self.expected_state_revision < 1:
            raise ValueError("expected state revision must be positive")


@dataclass(frozen=True, slots=True)
class EditSessionStart:
    session_key: str
    document_id: str
    client_request_id: str | None = None

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)


@dataclass(frozen=True, slots=True)
class EditSessionMutation:
    session_key: str
    edit_session_id: str
    expected_state_revision: int

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        _identity(self.edit_session_id, "edit session id")
        if self.expected_state_revision < 1:
            raise ValueError("expected edit session state revision must be positive")


@dataclass(frozen=True, slots=True)
class SourceRead:
    session_key: str
    document_id: str
    revision_id: str | None = None

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)


@dataclass(frozen=True, slots=True)
class SourceEdit:
    start_offset: int
    end_offset: int
    replacement: str

    def __post_init__(self) -> None:
        if self.start_offset < 0 or self.end_offset < self.start_offset:
            raise ValueError("source edit range is invalid")


@dataclass(frozen=True, slots=True)
class SourcePatch:
    session_key: str
    document_id: str
    expected_head_revision_id: str
    expected_source_sha256: str
    expected_state_revision: int
    edits: Sequence[SourceEdit]
    request_id: str
    offset_encoding: str = "unicode-code-point"
    edit_session_id: str | None = None
    expected_edit_session_state_revision: int | None = None
    expected_last_saved_revision_id: str | None = None

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)
        _identity(self.expected_head_revision_id, "expected head revision id")
        _identity(self.expected_source_sha256, "expected source digest")
        _identity(self.request_id, "request id")
        if self.expected_state_revision < 1:
            raise ValueError("expected state revision must be positive")
        if not self.edits:
            raise ValueError("at least one source edit is required")
        edit_session_fields = (
            self.edit_session_id,
            self.expected_edit_session_state_revision,
            self.expected_last_saved_revision_id,
        )
        if any(value is not None for value in edit_session_fields) and not all(
            value is not None for value in edit_session_fields
        ):
            raise ValueError("edit session fencing fields must be supplied together")


@dataclass(frozen=True, slots=True)
class PromptAnnotationSelection:
    selection_id: str
    tag_name: str
    element_path: str
    element_proof_sha256: str
    dom_sha256: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.selection_id, "selection id"),
            (self.tag_name, "selection tag name"),
            (self.element_path, "selection element path"),
            (self.element_proof_sha256, "selection element proof"),
        ):
            _identity(value, label)


@dataclass(frozen=True, slots=True)
class PromptAnnotationQuery:
    session_key: str
    document_id: str | None = None
    status: str = "draft"
    limit: int = 500

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        if self.document_id is not None:
            _identity(self.document_id, "document id")
        _identity(self.status, "annotation status")
        if self.limit < 1 or self.limit > 500:
            raise ValueError("annotation page limit must be between 1 and 500")


@dataclass(frozen=True, slots=True)
class PromptAnnotationCreate:
    session_key: str
    annotation_id: str
    document_id: str
    selection: PromptAnnotationSelection
    revision_id: str | None = None
    body: str | None = None

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)
        _identity(self.annotation_id, "annotation id")
        if self.revision_id is not None:
            _identity(self.revision_id, "revision id")


@dataclass(frozen=True, slots=True)
class PromptAnnotationIdentity:
    session_key: str
    annotation_id: str

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        _identity(self.annotation_id, "annotation id")


@dataclass(frozen=True, slots=True)
class PromptAnnotationMutation:
    session_key: str
    annotation_id: str
    expected_state_revision: int
    body: str | None = None

    def __post_init__(self) -> None:
        PromptAnnotationIdentity(self.session_key, self.annotation_id)
        if self.expected_state_revision < 1:
            raise ValueError("expected annotation state revision must be positive")


@dataclass(frozen=True, slots=True)
class WorkbenchResourceRef:
    resource_type: str
    resource_id: str

    def __post_init__(self) -> None:
        if self.resource_type not in {"attachment", "document", "deliverable", "url"}:
            raise ValueError("unsupported Workbench resource type")
        _identity(self.resource_id, "resource id")


@dataclass(frozen=True, slots=True)
class WorkbenchResourceQuery:
    session_key: str
    resource: WorkbenchResourceRef

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")


@dataclass(frozen=True, slots=True)
class WorkbenchResourceListQuery:
    session_key: str
    resource_types: Sequence[str] = (
        "document",
        "attachment",
        "deliverable",
        "url",
    )
    limit: int = 100
    cursor: str | None = None

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        if not self.resource_types or any(
            item not in {"attachment", "document", "deliverable", "url"}
            for item in self.resource_types
        ):
            raise ValueError("resource types are invalid")
        if self.limit < 1:
            raise ValueError("resource page limit must be positive")


@dataclass(frozen=True, slots=True)
class WorkbenchResourceOpen:
    session_key: str
    resource: WorkbenchResourceRef
    idempotency_key: str | None = None
    expected_sha256: str | None = None
    intent: str = "edit-current"

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        if self.intent != "edit-current":
            raise ValueError("unsupported resource open intent")
        if self.idempotency_key is not None:
            _identity(self.idempotency_key, "idempotency key")


@dataclass(frozen=True, slots=True)
class WorkbenchPreviewCreate:
    session_key: str
    resource: WorkbenchResourceRef
    mode: str = "isolated"

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        if self.mode != "isolated":
            raise ValueError("unsupported Workbench preview mode")


@dataclass(frozen=True, slots=True)
class DocumentImport:
    session_key: str
    source: WorkbenchResourceRef
    idempotency_key: str
    expected_sha256: str | None = None
    client_request_id: str | None = None
    name: str | None = None
    mode: str = "copy"

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        _identity(self.idempotency_key, "idempotency key")
        if self.mode != "copy":
            raise ValueError("unsupported document import mode")


@dataclass(frozen=True, slots=True)
class DocumentPublish:
    session_key: str
    document_id: str
    revision_id: str
    idempotency_key: str
    client_request_id: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        DocumentIdentity(self.session_key, self.document_id)
        _identity(self.revision_id, "revision id")
        _identity(self.idempotency_key, "idempotency key")


@dataclass(frozen=True, slots=True)
class MutationResolution:
    session_key: str
    operation: str
    request_id: str
    document_id: str | None = None

    def __post_init__(self) -> None:
        _identity(self.session_key, "session key")
        _identity(self.operation, "mutation operation")
        _identity(self.request_id, "mutation request id")
        if self.document_id is not None:
            _identity(self.document_id, "document id")


class ArtifactCatalogPort(Protocol):
    async def list_artifacts(self, query: ArtifactCatalogQuery) -> Mapping[str, Any]: ...

    async def get_artifact(self, identity: ArtifactIdentity) -> Mapping[str, Any]: ...


class DocumentWorkspacePort(Protocol):
    async def capabilities(
        self, query: DocumentCapabilitiesQuery
    ) -> Mapping[str, Any]: ...

    async def open_document(self, command: DocumentOpen) -> Mapping[str, Any]: ...

    async def list_documents(
        self, query: SessionDocumentsQuery
    ) -> Mapping[str, Any]: ...

    async def get_document(self, identity: DocumentIdentity) -> Mapping[str, Any]: ...

    async def rename_document(self, command: DocumentRename) -> Mapping[str, Any]: ...

    async def close_document(self, identity: DocumentIdentity) -> Mapping[str, Any]: ...


class RevisionHistoryPort(Protocol):
    async def list_revisions(self, query: RevisionListQuery) -> Mapping[str, Any]: ...

    async def restore_revision(self, command: RevisionRestore) -> Mapping[str, Any]: ...


class ChangeHistoryPort(Protocol):
    async def list_changes(self, query: ChangeListQuery) -> Mapping[str, Any]: ...

    async def get_change(self, identity: ChangeIdentity) -> Mapping[str, Any]: ...

    async def revert_change(self, command: ChangeRevert) -> Mapping[str, Any]: ...


class DocumentEditSessionPort(Protocol):
    async def start_edit_session(self, command: EditSessionStart) -> Mapping[str, Any]: ...

    async def heartbeat_edit_session(
        self, command: EditSessionMutation
    ) -> Mapping[str, Any]: ...

    async def close_edit_session(self, command: EditSessionMutation) -> Mapping[str, Any]: ...


class DocumentSourcePort(Protocol):
    async def read_source(self, query: SourceRead) -> Mapping[str, Any]: ...

    async def patch_source(self, command: SourcePatch) -> Mapping[str, Any]: ...


class PromptAnnotationPort(Protocol):
    async def list_annotations(
        self, query: PromptAnnotationQuery
    ) -> Mapping[str, Any]: ...

    async def create_annotation(
        self, command: PromptAnnotationCreate
    ) -> Mapping[str, Any]: ...

    async def focus_annotation(
        self, identity: PromptAnnotationIdentity
    ) -> Mapping[str, Any]: ...

    async def update_annotation(
        self, command: PromptAnnotationMutation
    ) -> Mapping[str, Any]: ...

    async def discard_annotation(
        self, command: PromptAnnotationMutation
    ) -> Mapping[str, Any]: ...


class WorkbenchResourcePort(Protocol):
    async def list_resources(
        self, query: WorkbenchResourceListQuery
    ) -> Mapping[str, Any]: ...

    async def get_resource(
        self, query: WorkbenchResourceQuery
    ) -> Mapping[str, Any]: ...

    async def open_resource(
        self, command: WorkbenchResourceOpen
    ) -> Mapping[str, Any]: ...


class ResourcePreviewPort(Protocol):
    async def create_preview(
        self, command: WorkbenchPreviewCreate
    ) -> Mapping[str, Any]: ...


class DocumentTransferPort(Protocol):
    async def import_document(self, command: DocumentImport) -> Mapping[str, Any]: ...

    async def publish_document(self, command: DocumentPublish) -> Mapping[str, Any]: ...


class MutationOutcomePort(Protocol):
    async def resolve_mutation(
        self, query: MutationResolution
    ) -> Mapping[str, Any]: ...


class ArtifactCatalog:
    def __init__(self, port: ArtifactCatalogPort) -> None:
        self._port = port

    async def list(self, query: ArtifactCatalogQuery) -> Mapping[str, Any]:
        return await self._port.list_artifacts(query)

    async def get(self, identity: ArtifactIdentity) -> Mapping[str, Any]:
        return await self._port.get_artifact(identity)


class DocumentWorkspace:
    def __init__(self, port: DocumentWorkspacePort) -> None:
        self._port = port

    async def capabilities(self, query: DocumentCapabilitiesQuery) -> Mapping[str, Any]:
        return await self._port.capabilities(query)

    async def open(self, command: DocumentOpen) -> Mapping[str, Any]:
        return await self._port.open_document(command)

    async def list(self, query: SessionDocumentsQuery) -> Mapping[str, Any]:
        return await self._port.list_documents(query)

    async def get(self, identity: DocumentIdentity) -> Mapping[str, Any]:
        return await self._port.get_document(identity)

    async def rename(self, command: DocumentRename) -> Mapping[str, Any]:
        return await self._port.rename_document(command)

    async def close(self, identity: DocumentIdentity) -> Mapping[str, Any]:
        return await self._port.close_document(identity)


class RevisionHistory:
    def __init__(self, port: RevisionHistoryPort) -> None:
        self._port = port

    async def list(self, query: RevisionListQuery) -> Mapping[str, Any]:
        return await self._port.list_revisions(query)

    async def restore(self, command: RevisionRestore) -> Mapping[str, Any]:
        return await self._port.restore_revision(command)


class ChangeHistory:
    def __init__(self, port: ChangeHistoryPort) -> None:
        self._port = port

    async def list(self, query: ChangeListQuery) -> Mapping[str, Any]:
        return await self._port.list_changes(query)

    async def get(self, identity: ChangeIdentity) -> Mapping[str, Any]:
        return await self._port.get_change(identity)

    async def revert(self, command: ChangeRevert) -> Mapping[str, Any]:
        return await self._port.revert_change(command)


class DocumentEditSession:
    def __init__(self, port: DocumentEditSessionPort) -> None:
        self._port = port

    async def start(self, command: EditSessionStart) -> Mapping[str, Any]:
        return await self._port.start_edit_session(command)

    async def heartbeat(self, command: EditSessionMutation) -> Mapping[str, Any]:
        return await self._port.heartbeat_edit_session(command)

    async def close(self, command: EditSessionMutation) -> Mapping[str, Any]:
        return await self._port.close_edit_session(command)


class DocumentSource:
    def __init__(self, port: DocumentSourcePort) -> None:
        self._port = port

    async def read(self, query: SourceRead) -> Mapping[str, Any]:
        return await self._port.read_source(query)

    async def patch(self, command: SourcePatch) -> Mapping[str, Any]:
        return await self._port.patch_source(command)


class PromptAnnotationApplication:
    def __init__(self, port: PromptAnnotationPort) -> None:
        self._port = port

    async def list(self, query: PromptAnnotationQuery) -> Mapping[str, Any]:
        return await self._port.list_annotations(query)

    async def create(self, command: PromptAnnotationCreate) -> Mapping[str, Any]:
        return await self._port.create_annotation(command)

    async def focus(self, identity: PromptAnnotationIdentity) -> Mapping[str, Any]:
        return await self._port.focus_annotation(identity)

    async def update(self, command: PromptAnnotationMutation) -> Mapping[str, Any]:
        return await self._port.update_annotation(command)

    async def discard(self, command: PromptAnnotationMutation) -> Mapping[str, Any]:
        return await self._port.discard_annotation(command)


class WorkbenchResourceApplication:
    def __init__(self, port: WorkbenchResourcePort) -> None:
        self._port = port

    async def list(self, query: WorkbenchResourceListQuery) -> Mapping[str, Any]:
        return await self._port.list_resources(query)

    async def get(self, query: WorkbenchResourceQuery) -> Mapping[str, Any]:
        return await self._port.get_resource(query)

    async def open(self, command: WorkbenchResourceOpen) -> Mapping[str, Any]:
        return await self._port.open_resource(command)


class ResourcePreviewApplication:
    def __init__(self, port: ResourcePreviewPort) -> None:
        self._port = port

    async def create(self, command: WorkbenchPreviewCreate) -> Mapping[str, Any]:
        return await self._port.create_preview(command)


class DocumentTransferApplication:
    def __init__(self, port: DocumentTransferPort) -> None:
        self._port = port

    async def import_document(self, command: DocumentImport) -> Mapping[str, Any]:
        return await self._port.import_document(command)

    async def publish_document(self, command: DocumentPublish) -> Mapping[str, Any]:
        return await self._port.publish_document(command)


class MutationOutcomeApplication:
    def __init__(self, port: MutationOutcomePort) -> None:
        self._port = port

    async def resolve(self, query: MutationResolution) -> Mapping[str, Any]:
        return await self._port.resolve_mutation(query)


@dataclass(frozen=True, slots=True)
class ArtifactWorkbench:
    artifacts: ArtifactCatalog
    documents: DocumentWorkspace
    revisions: RevisionHistory
    changes: ChangeHistory
    edit_sessions: DocumentEditSession
    source: DocumentSource
    prompt_annotations: PromptAnnotationApplication
    resources: WorkbenchResourceApplication
    previews: ResourcePreviewApplication
    transfers: DocumentTransferApplication
    mutation_outcomes: MutationOutcomeApplication


__all__ = [name for name in globals() if not name.startswith("_")]
