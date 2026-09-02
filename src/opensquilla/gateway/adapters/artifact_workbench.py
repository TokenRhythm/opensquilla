"""Gateway Adapter for the transport-neutral Artifact Workbench applications."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, cast

from opensquilla.application.artifact_workbench import (
    ArtifactCatalog,
    ArtifactCatalogPort,
    ArtifactCatalogQuery,
    ArtifactIdentity,
    ChangeHistory,
    ChangeHistoryPort,
    ChangeIdentity,
    ChangeListQuery,
    ChangeRevert,
    DocumentCapabilitiesQuery,
    DocumentEditSession,
    DocumentEditSessionPort,
    DocumentIdentity,
    DocumentImport,
    DocumentOpen,
    DocumentPublish,
    DocumentRename,
    DocumentSource,
    DocumentSourcePort,
    DocumentTransferApplication,
    DocumentTransferPort,
    DocumentWorkspace,
    DocumentWorkspacePort,
    EditSessionMutation,
    EditSessionStart,
    MutationOutcomeApplication,
    MutationOutcomePort,
    MutationResolution,
    PromptAnnotationApplication,
    PromptAnnotationCreate,
    PromptAnnotationIdentity,
    PromptAnnotationMutation,
    PromptAnnotationPort,
    PromptAnnotationQuery,
    PromptAnnotationSelection,
    ResourcePreviewApplication,
    ResourcePreviewPort,
    RevisionHistory,
    RevisionHistoryPort,
    RevisionListQuery,
    RevisionRestore,
    SessionDocumentsQuery,
    SourceEdit,
    SourcePatch,
    SourceRead,
    WorkbenchPreviewCreate,
    WorkbenchResourceApplication,
    WorkbenchResourceListQuery,
    WorkbenchResourceOpen,
    WorkbenchResourcePort,
    WorkbenchResourceQuery,
    WorkbenchResourceRef,
)
from opensquilla.gateway.adapters.artifact_workbench_contract import (
    ARTIFACT_WORKBENCH_CONTRACT_METHODS,
)
from opensquilla.gateway.rpc import RpcContext

WorkbenchHandler = Callable[[dict[str, Any] | None, RpcContext], Awaitable[dict[str, Any]]]


class _CallbackPort:
    """Keep RpcContext in the Adapter while preserving the proven implementation."""

    def __init__(
        self,
        implementation: WorkbenchHandler,
        params: dict[str, Any] | None,
        ctx: RpcContext,
    ) -> None:
        self._implementation = implementation
        self._params = params
        self._ctx = ctx

    async def _call(self, request: object) -> Mapping[str, Any]:
        del request
        return await self._implementation(self._params, self._ctx)

    async def list_artifacts(self, value: ArtifactCatalogQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def get_artifact(self, value: ArtifactIdentity) -> Mapping[str, Any]:
        return await self._call(value)

    async def capabilities(self, value: DocumentCapabilitiesQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def open_document(self, value: DocumentOpen) -> Mapping[str, Any]:
        return await self._call(value)

    async def list_documents(self, value: SessionDocumentsQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def get_document(self, value: DocumentIdentity) -> Mapping[str, Any]:
        return await self._call(value)

    async def rename_document(self, value: DocumentRename) -> Mapping[str, Any]:
        return await self._call(value)

    async def close_document(self, value: DocumentIdentity) -> Mapping[str, Any]:
        return await self._call(value)

    async def list_revisions(self, value: RevisionListQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def restore_revision(self, value: RevisionRestore) -> Mapping[str, Any]:
        return await self._call(value)

    async def list_changes(self, value: ChangeListQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def get_change(self, value: ChangeIdentity) -> Mapping[str, Any]:
        return await self._call(value)

    async def revert_change(self, value: ChangeRevert) -> Mapping[str, Any]:
        return await self._call(value)

    async def start_edit_session(self, value: EditSessionStart) -> Mapping[str, Any]:
        return await self._call(value)

    async def heartbeat_edit_session(self, value: EditSessionMutation) -> Mapping[str, Any]:
        return await self._call(value)

    async def close_edit_session(self, value: EditSessionMutation) -> Mapping[str, Any]:
        return await self._call(value)

    async def read_source(self, value: SourceRead) -> Mapping[str, Any]:
        return await self._call(value)

    async def patch_source(self, value: SourcePatch) -> Mapping[str, Any]:
        return await self._call(value)

    async def list_annotations(self, value: PromptAnnotationQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def create_annotation(self, value: PromptAnnotationCreate) -> Mapping[str, Any]:
        return await self._call(value)

    async def focus_annotation(self, value: PromptAnnotationIdentity) -> Mapping[str, Any]:
        return await self._call(value)

    async def update_annotation(self, value: PromptAnnotationMutation) -> Mapping[str, Any]:
        return await self._call(value)

    async def discard_annotation(self, value: PromptAnnotationMutation) -> Mapping[str, Any]:
        return await self._call(value)

    async def list_resources(self, value: WorkbenchResourceListQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def get_resource(self, value: WorkbenchResourceQuery) -> Mapping[str, Any]:
        return await self._call(value)

    async def open_resource(self, value: WorkbenchResourceOpen) -> Mapping[str, Any]:
        return await self._call(value)

    async def create_preview(self, value: WorkbenchPreviewCreate) -> Mapping[str, Any]:
        return await self._call(value)

    async def import_document(self, value: DocumentImport) -> Mapping[str, Any]:
        return await self._call(value)

    async def publish_document(self, value: DocumentPublish) -> Mapping[str, Any]:
        return await self._call(value)

    async def resolve_mutation(self, value: MutationResolution) -> Mapping[str, Any]:
        return await self._call(value)


class GatewayArtifactWorkbenchAdapter:
    """Parse v4 wire input into narrow Workbench use cases."""

    def __init__(
        self,
        ctx: RpcContext,
        implementation: WorkbenchHandler,
        params: dict[str, Any] | None,
    ) -> None:
        self._port = _CallbackPort(implementation, params, ctx)
        self._params = params if isinstance(params, dict) else {}

    @classmethod
    def bind(cls, method: str, implementation: WorkbenchHandler) -> WorkbenchHandler:
        if method not in ARTIFACT_WORKBENCH_CONTRACT_METHODS:
            raise ValueError(f"unsupported Artifact Workbench method: {method}")

        async def handle(params: dict[str, Any] | None, ctx: RpcContext) -> dict[str, Any]:
            return await cls(ctx, implementation, params).dispatch(method)

        return handle

    async def dispatch(self, method: str) -> dict[str, Any]:
        p = self._params
        port = self._port
        if method == "artifacts.list":
            result = await ArtifactCatalog(cast(ArtifactCatalogPort, port)).list(
                ArtifactCatalogQuery(
                    self._text("sessionKey"),
                    self._limit("limit", 100, maximum=200),
                    self._optional_text("before"),
                )
            )
        elif method == "artifacts.get":
            result = await ArtifactCatalog(cast(ArtifactCatalogPort, port)).get(
                ArtifactIdentity(self._text("sessionKey"), self._text("artifactId"))
            )
        elif method == "artifacts.edit.capabilities":
            session_key = self._optional_text("sessionKey")
            document_id = self._optional_text("documentId")
            if session_key is None or document_id is None:
                session_key = document_id = None
            result = await DocumentWorkspace(cast(DocumentWorkspacePort, port)).capabilities(
                DocumentCapabilitiesQuery(session_key, document_id)
            )
        elif method == "artifacts.documents.open":
            result = await DocumentWorkspace(cast(DocumentWorkspacePort, port)).open(
                DocumentOpen(self._text("sessionKey"), self._text("artifactId"))
            )
        elif method == "artifacts.documents.list":
            result = await DocumentWorkspace(cast(DocumentWorkspacePort, port)).list(
                SessionDocumentsQuery(self._text("sessionKey"), self._limit("limit", 100))
            )
        elif method == "artifacts.documents.get":
            result = await DocumentWorkspace(cast(DocumentWorkspacePort, port)).get(
                self._document_identity()
            )
        elif method == "artifacts.documents.rename":
            result = await DocumentWorkspace(cast(DocumentWorkspacePort, port)).rename(
                DocumentRename(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._positive("expectedStateRevision"),
                    self._text("name"),
                )
            )
        elif method == "artifacts.documents.close":
            result = await DocumentWorkspace(cast(DocumentWorkspacePort, port)).close(
                self._document_identity()
            )
        elif method == "documents.editSessions.start":
            result = await DocumentEditSession(cast(DocumentEditSessionPort, port)).start(
                EditSessionStart(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._optional_text("clientRequestId"),
                )
            )
        elif method in {
            "documents.editSessions.heartbeat",
            "documents.editSessions.close",
        }:
            command = EditSessionMutation(
                self._text("sessionKey"),
                self._text("editSessionId"),
                self._positive("expectedStateRevision"),
            )
            application = DocumentEditSession(cast(DocumentEditSessionPort, port))
            result = (
                await application.heartbeat(command)
                if method.endswith("heartbeat")
                else await application.close(command)
            )
        elif method == "artifacts.revisions.list":
            result = await RevisionHistory(cast(RevisionHistoryPort, port)).list(
                RevisionListQuery(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._limit("limit", 100),
                )
            )
        elif method == "artifacts.revisions.restore":
            result = await RevisionHistory(cast(RevisionHistoryPort, port)).restore(
                RevisionRestore(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._text("revisionId"),
                    self._text("expectedHeadRevisionId"),
                    self._positive("expectedStateRevision"),
                    self._manual_request_id(),
                )
            )
        elif method == "artifacts.changes.list":
            result = await ChangeHistory(cast(ChangeHistoryPort, port)).list(
                ChangeListQuery(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._limit("limit", 100),
                )
            )
        elif method == "artifacts.changes.get":
            result = await ChangeHistory(cast(ChangeHistoryPort, port)).get(
                ChangeIdentity(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._text("changeSetId"),
                )
            )
        elif method == "artifacts.changes.revert":
            result = await ChangeHistory(cast(ChangeHistoryPort, port)).revert(
                ChangeRevert(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._text("changeSetId"),
                    self._text("expectedHeadRevisionId"),
                    self._positive("expectedStateRevision"),
                    self._manual_request_id(),
                )
            )
        elif method == "artifacts.prompt_annotations.list":
            result = await PromptAnnotationApplication(cast(PromptAnnotationPort, port)).list(
                PromptAnnotationQuery(
                    self._text("sessionKey"),
                    self._optional_text("documentId"),
                    self._optional_text("status") or "draft",
                    self._limit("limit", 500, maximum=500),
                )
            )
        elif method == "artifacts.prompt_annotations.create":
            selection = self._mapping("selection")
            raw_body = p.get("body")
            if raw_body is not None and not isinstance(raw_body, str):
                raise ValueError("body must be a string")
            result = await PromptAnnotationApplication(cast(PromptAnnotationPort, port)).create(
                PromptAnnotationCreate(
                    self._text("sessionKey"),
                    self._text("annotationId"),
                    self._text("documentId"),
                    PromptAnnotationSelection(
                        self._mapping_text(selection, "selectionId"),
                        self._mapping_text(selection, "tagName"),
                        self._mapping_text(selection, "elementPath"),
                        self._mapping_text(selection, "elementProofSha256"),
                        self._mapping_optional_text(selection, "domSha256"),
                    ),
                    self._optional_text("revisionId"),
                    cast(str | None, raw_body),
                )
            )
        elif method == "artifacts.prompt_annotations.focus":
            result = await PromptAnnotationApplication(cast(PromptAnnotationPort, port)).focus(
                PromptAnnotationIdentity(self._text("sessionKey"), self._text("annotationId"))
            )
        elif method in {
            "artifacts.prompt_annotations.update",
            "artifacts.prompt_annotations.discard",
        }:
            annotation_command = PromptAnnotationMutation(
                self._text("sessionKey"),
                self._text("annotationId"),
                self._positive("expectedStateRevision"),
                self._optional_text("body"),
            )
            annotation_application = PromptAnnotationApplication(cast(PromptAnnotationPort, port))
            result = (
                await annotation_application.update(annotation_command)
                if method.endswith("update")
                else await annotation_application.discard(annotation_command)
            )
        elif method == "artifacts.source.read":
            result = await DocumentSource(cast(DocumentSourcePort, port)).read(
                SourceRead(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._optional_text("revisionId"),
                )
            )
        elif method == "artifacts.source.patch":
            raw_edits = p.get("patches")
            if not isinstance(raw_edits, list):
                raise ValueError("patches must be a list")
            edits = tuple(
                SourceEdit(
                    self._mapping_int(self._as_mapping(item, "patch"), "startOffset"),
                    self._mapping_int(self._as_mapping(item, "patch"), "endOffset"),
                    self._mapping_text(self._as_mapping(item, "patch"), "replacement", strip=False),
                )
                for item in raw_edits
            )
            result = await DocumentSource(cast(DocumentSourcePort, port)).patch(
                SourcePatch(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._text("expectedHeadRevisionId"),
                    self._text("expectedSourceSha256"),
                    self._positive("expectedStateRevision"),
                    edits,
                    self._manual_request_id(),
                    self._optional_text("offsetEncoding") or "unicode-code-point",
                    self._optional_text("editSessionId"),
                    self._optional_positive("expectedEditSessionStateRevision"),
                    self._optional_text("expectedLastSavedRevisionId"),
                )
            )
        elif method == "workbench.resources.list":
            raw_types = p.get("types")
            resource_types = (
                tuple(item for item in raw_types if isinstance(item, str))
                if isinstance(raw_types, list)
                else ("document", "attachment", "deliverable", "url")
            )
            result = await WorkbenchResourceApplication(cast(WorkbenchResourcePort, port)).list(
                WorkbenchResourceListQuery(
                    self._text("sessionKey"),
                    resource_types,
                    self._limit("limit", 100, maximum=500),
                    self._optional_text("cursor"),
                )
            )
        elif method == "workbench.resources.get":
            result = await WorkbenchResourceApplication(cast(WorkbenchResourcePort, port)).get(
                WorkbenchResourceQuery(self._text("sessionKey"), self._resource_ref())
            )
        elif method == "workbench.resources.open":
            result = await WorkbenchResourceApplication(cast(WorkbenchResourcePort, port)).open(
                WorkbenchResourceOpen(
                    self._text("sessionKey"),
                    self._resource_ref(),
                    self._optional_request_id(),
                    self._optional_text("expectedSha256"),
                    self._optional_text("intent") or "edit-current",
                )
            )
        elif method == "workbench.previews.create":
            result = await ResourcePreviewApplication(cast(ResourcePreviewPort, port)).create(
                WorkbenchPreviewCreate(
                    self._text("sessionKey"),
                    self._resource_ref(),
                    self._optional_text("mode") or "isolated",
                )
            )
        elif method == "documents.import":
            result = await DocumentTransferApplication(
                cast(DocumentTransferPort, port)
            ).import_document(
                DocumentImport(
                    self._text("sessionKey"),
                    self._resource_ref("source"),
                    self._request_id(),
                    self._optional_text("expectedSha256"),
                    self._optional_text("clientRequestId"),
                    self._optional_text("name"),
                    self._optional_text("mode") or "copy",
                )
            )
        elif method == "documents.publish":
            result = await DocumentTransferApplication(
                cast(DocumentTransferPort, port)
            ).publish_document(
                DocumentPublish(
                    self._text("sessionKey"),
                    self._text("documentId"),
                    self._text("revisionId"),
                    self._request_id(),
                    self._optional_text("clientRequestId"),
                    self._optional_text("name"),
                )
            )
        elif method == "artifacts.mutations.resolve":
            result = await MutationOutcomeApplication(cast(MutationOutcomePort, port)).resolve(
                MutationResolution(
                    self._text("sessionKey"),
                    self._text("operation"),
                    self._request_id(),
                    self._optional_text("documentId"),
                )
            )
        else:
            raise ValueError(f"unsupported Artifact Workbench method: {method}")
        return dict(result)

    def _document_identity(self) -> DocumentIdentity:
        return DocumentIdentity(self._text("sessionKey"), self._text("documentId"))

    def _resource_ref(self, field: str | None = None) -> WorkbenchResourceRef:
        if field is None:
            value = self._params.get("resourceRef", self._params.get("resource"))
        else:
            value = self._params.get(field)
        raw = self._as_mapping(value, field or "resourceRef")
        resource_type = self._mapping_text(raw, "type")
        id_field = {
            "attachment": "attachmentId",
            "document": "documentId",
            "deliverable": "artifactId",
            "url": "urlId",
        }.get(resource_type)
        resource_id = self._mapping_optional_text(raw, id_field or "id")
        resource_id = resource_id or self._mapping_text(raw, "id")
        return WorkbenchResourceRef(resource_type, resource_id)

    def _request_id(self) -> str:
        value = self._optional_request_id()
        if value is None:
            raise ValueError("idempotencyKey, clientRequestId, or requestId is required")
        return value

    def _manual_request_id(self) -> str:
        request_id = self._optional_text("clientRequestId")
        if request_id is not None:
            return request_id
        canonical = json.dumps(
            self._params,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"legacy-{hashlib.sha256(canonical).hexdigest()}"

    def _optional_request_id(self) -> str | None:
        return next(
            (
                value
                for name in ("idempotencyKey", "clientRequestId", "requestId")
                if (value := self._optional_text(name)) is not None
            ),
            None,
        )

    def _text(self, name: str) -> str:
        return self._mapping_text(self._params, name)

    def _optional_text(self, name: str) -> str | None:
        return self._mapping_optional_text(self._params, name)

    def _mapping(self, name: str) -> Mapping[str, Any]:
        return self._as_mapping(self._params.get(name), name)

    @staticmethod
    def _as_mapping(value: object, name: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{name} must be an object")
        return cast(Mapping[str, Any], value)

    @classmethod
    def _mapping_text(cls, values: Mapping[str, Any], name: str, *, strip: bool = True) -> str:
        value = values.get(name)
        if not isinstance(value, str) or (strip and not value.strip()):
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip() if strip else value

    @classmethod
    def _mapping_optional_text(cls, values: Mapping[str, Any], name: str) -> str | None:
        if name not in values or values[name] is None:
            return None
        return cls._mapping_text(values, name)

    @staticmethod
    def _mapping_int(values: Mapping[str, Any], name: str) -> int:
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer")
        return value

    def _positive(self, name: str) -> int:
        value = self._mapping_int(self._params, name)
        if value < 1:
            raise ValueError(f"{name} must be positive")
        return value

    def _optional_positive(self, name: str) -> int | None:
        if name not in self._params or self._params[name] is None:
            return None
        return self._positive(name)

    def _limit(self, name: str, default: int, *, maximum: int | None = None) -> int:
        value = self._params.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            return default
        return min(value, maximum) if maximum is not None else value


__all__ = ["GatewayArtifactWorkbenchAdapter", "WorkbenchHandler"]
