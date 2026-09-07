"""Gateway storage adapters for Workbench HTTP content and upload staging."""

from __future__ import annotations

import hashlib
from typing import Any, Protocol

from opensquilla.application.artifact_workbench import (
    ArtifactContentPort,
    ArtifactContentQuery,
    AttachmentClaimError,
    AttachmentContentQuery,
    AttachmentMimePolicyPort,
    AttachmentStagingPort,
    ContentIntegrityError,
    ContentMaterial,
    ContentNotFoundError,
    DocumentContentQuery,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
)
from opensquilla.attachment_refs import transcript_material_path
from opensquilla.contracts.attachment_sniff import sniff_mime_from_bytes
from opensquilla.contracts.attachments import (
    ALLOWED_MEDIA_TYPES,
    MSG_MIME,
    OPAQUE_MIME,
    attachment_category,
    normalize_attachment_mime,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import media_root_from_config, native_io_path


class UploadStagingStore(Protocol):
    async def put_with_expiry(self, name: str, mime: str, payload: bytes) -> tuple[str, float]: ...


class GatewayArtifactContentPort(ArtifactContentPort):
    def __init__(
        self,
        config: GatewayConfig,
        *,
        session_manager: Any = None,
    ) -> None:
        self._config = config
        self._session_manager = session_manager

    async def artifact_content(self, query: ArtifactContentQuery) -> ContentMaterial:
        session_id = await self._session_id(query.session_key)
        if session_id is None:
            raise ContentNotFoundError("artifact not found")
        store = ArtifactStore(media_root_from_config(self._config))
        try:
            ref, path = store.resolve_for_download(query.artifact_id, session_id=session_id)
            if query.thumbnail:
                thumbnail = store.resolve_thumbnail_for_download(
                    query.artifact_id, session_id=session_id
                )
                if thumbnail is not None:
                    _thumbnail_ref, thumbnail_path = thumbnail
                    return ContentMaterial(native_io_path(thumbnail_path), "image/webp")
        except ArtifactIntegrityError as exc:
            raise ContentIntegrityError(str(exc)) from exc
        except (ArtifactNotFoundError, ValueError) as exc:
            raise ContentNotFoundError("artifact not found") from exc
        return ContentMaterial(native_io_path(path), ref.mime, ref.name)

    async def document_content(self, query: DocumentContentQuery) -> ContentMaterial:
        session_id = await self._session_id(query.session_key)
        storage = get_session_storage(self._session_manager)
        if session_id is None or storage is None:
            raise ContentNotFoundError("artifact document not found")
        try:
            service = await ArtifactSessionService.from_session_storage(storage)
            document = await service.get_document(query.document_id)
            if document.session_key != query.session_key or document.session_id != session_id:
                raise ArtifactSessionNotFoundError("artifact document not found")
            revision_id = query.revision_id or document.head_revision_id
            revision = await service.get_revision(revision_id)
            if revision.document_id != document.document_id:
                raise ArtifactSessionNotFoundError("artifact revision not found")
            ref, path = ArtifactStore(media_root_from_config(self._config)).resolve_for_download(
                revision.artifact_id, session_id=session_id
            )
        except ArtifactIntegrityError as exc:
            raise ContentIntegrityError(str(exc)) from exc
        except (
            ArtifactSessionNotFoundError,
            ArtifactNotFoundError,
            ValueError,
        ) as exc:
            raise ContentNotFoundError("artifact document not found") from exc
        filename = document.name if revision_id == document.head_revision_id else revision.filename
        return ContentMaterial(native_io_path(path), ref.mime, filename)

    async def attachment_content(self, query: AttachmentContentQuery) -> ContentMaterial:
        session_id = await self._session_id(query.session_key)
        if session_id is None:
            raise ContentNotFoundError("attachment not found")
        try:
            path = transcript_material_path(
                media_root_from_config(self._config), session_id, query.sha256
            )
        except ValueError as exc:
            raise ContentNotFoundError("attachment not found") from exc
        native_path = native_io_path(path)
        if not native_path.exists() or not native_path.is_file():
            raise ContentNotFoundError("attachment not found")
        try:
            actual_sha = hashlib.sha256(native_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise ContentNotFoundError("attachment not found") from exc
        if actual_sha != query.sha256:
            raise ContentIntegrityError("attachment integrity check failed")
        return ContentMaterial(native_path, "application/octet-stream")

    async def _session_id(self, session_key: str) -> str | None:
        if self._session_manager is None:
            return session_key
        get_session = getattr(self._session_manager, "get_session", None)
        if not callable(get_session):
            return session_key
        try:
            session = await get_session(session_key)
        except Exception:
            return None
        session_id = getattr(session, "session_id", None)
        return session_id if isinstance(session_id, str) and session_id else None


class GatewayAttachmentStagingPort(AttachmentStagingPort):
    def __init__(self, store: UploadStagingStore) -> None:
        self._store = store

    async def stage_attachment(
        self, *, filename: str, mime: str, payload: bytes
    ) -> tuple[str, float]:
        return await self._store.put_with_expiry(filename, mime, payload)


class GatewayAttachmentMimePolicy(AttachmentMimePolicyPort):
    def validate_claim(self, claimed_mime: str, *, accept_opaque: bool) -> str | None:
        normalized = normalize_attachment_mime(claimed_mime)
        if not accept_opaque and normalized is None:
            raise AttachmentClaimError("missing or invalid 'mime' / content-type")
        return normalized

    def resolve_mime(self, claimed_mime: str, payload: bytes, *, accept_opaque: bool) -> str:
        normalized = self.validate_claim(claimed_mime, accept_opaque=accept_opaque)
        if not accept_opaque:
            assert normalized is not None
            return normalized
        if normalized in ALLOWED_MEDIA_TYPES:
            return normalized
        sniffed = sniff_mime_from_bytes(payload)
        if sniffed in ALLOWED_MEDIA_TYPES and not (sniffed == MSG_MIME and normalized is not None):
            return sniffed
        return normalized or OPAQUE_MIME

    def is_opaque(self, mime: str) -> bool:
        return attachment_category(mime) == "opaque"


__all__ = [
    "GatewayArtifactContentPort",
    "GatewayAttachmentMimePolicy",
    "GatewayAttachmentStagingPort",
    "UploadStagingStore",
]
