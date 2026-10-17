"""Gateway storage adapters for Workbench HTTP content and upload staging."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, Protocol

from opensquilla.application.artifact_workbench import (
    ArtifactContentPort,
    ArtifactContentQuery,
    AttachmentClaimError,
    AttachmentContentQuery,
    AttachmentMimePolicyPort,
    AttachmentStagingPort,
    ContentExportError,
    ContentIntegrityError,
    ContentMaterial,
    ContentNotFoundError,
    DocumentContentQuery,
    WorkingFileMaterial,
    WorkingFileQuery,
)
from opensquilla.artifact_session import (
    ArtifactNotFoundError as ArtifactSessionNotFoundError,
)
from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStore,
    _read_regular_bundle_file,
)
from opensquilla.attachment_refs import transcript_material_path
from opensquilla.contracts.attachment_sniff import sniff_mime_from_bytes
from opensquilla.contracts.attachments import (
    ALLOWED_MEDIA_TYPES,
    IMAGE_ATTACHMENT_MIMES,
    MSG_MIME,
    OPAQUE_MIME,
    attachment_category,
    normalize_attachment_mime,
)
from opensquilla.gateway.config import GatewayConfig
from opensquilla.gateway.html_archive import HtmlZipExporter, capture_saved_html
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.paths import media_root_from_config, native_io_path


class UploadStagingStore(Protocol):
    async def put_with_expiry(self, name: str, mime: str, payload: bytes) -> tuple[str, float]: ...


def _verify_attachment_material(path: Path, expected_sha: str) -> None:
    """Verify the entire immutable material without buffering it on the event loop."""

    digest = hashlib.sha256()
    try:
        if not path.is_file():
            raise ContentNotFoundError("attachment not found")
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise ContentNotFoundError("attachment not found") from exc
    if digest.hexdigest() != expected_sha:
        raise ContentIntegrityError("attachment integrity check failed")


class GatewayArtifactContentPort(ArtifactContentPort):
    def __init__(
        self,
        config: GatewayConfig,
        *,
        session_manager: Any = None,
    ) -> None:
        self._config = config
        self._session_manager = session_manager
        self._zip_exports = HtmlZipExporter()

    async def working_file(self, query: WorkingFileQuery) -> WorkingFileMaterial | ContentMaterial:
        """Read an already-bound page; never materialize a copy or a version."""
        from opensquilla.agents.scope import resolve_agent_workspace_dir
        from opensquilla.artifact_session.working_files import checked_path, get_working_files
        from opensquilla.gateway.project_workspace_runtime import authoritative_project_run_context
        from opensquilla.gateway.rpc import RpcHandlerError
        from opensquilla.html_format import is_html_preview_path
        from opensquilla.project_workspaces import ProjectWorkspaceStateError
        from opensquilla.sandbox.path_validation import decide_path_access
        from opensquilla.sandbox.permissions import FileSystemPermissionProfile
        from opensquilla.session.keys import parse_agent_id

        storage = get_session_storage(self._session_manager)
        if storage is None:
            raise ContentNotFoundError("Working file unavailable")
        session = await storage.get_session(query.session_key)
        if session is None:
            raise ContentNotFoundError("Working file unavailable")
        service = await ArtifactSessionService.from_session_storage(storage)
        try:
            document = await service.get_document(query.document_id)
            if (document.session_key, document.session_id) != (
                query.session_key,
                session.session_id,
            ):
                raise ContentNotFoundError("Working file unavailable")
            binding = await get_working_files(service, query.document_id)
            if binding is None or binding.source_path is None:
                raise ContentNotFoundError("Working file unavailable")
            default = resolve_agent_workspace_dir(parse_agent_id(query.session_key), self._config)

            async def validate_workspace() -> None:
                current = await storage.get_session(query.session_key)
                if current is None or (current.session_id, current.epoch) != (
                    session.session_id,
                    session.epoch,
                ):
                    raise ContentNotFoundError("Working file unavailable")
                context, _ = await authoritative_project_run_context(
                    storage=storage,
                    session_manager=self._session_manager,
                    session=current,
                    config=self._config,
                    default_workspace=str(default) if default else None,
                )
                if not context.workspace or Path(context.workspace) != Path(binding.workspace):
                    raise ContentNotFoundError("Working workspace is no longer available")

            await validate_workspace()
            selected = query.page_path or binding.entrypoint
            if not is_html_preview_path(selected):
                raise ContentNotFoundError("Working file is not an HTML page")
            # The collector is the existing bounded, symlink-safe source reader.
            # Its member inventory, not a path supplied by the caller, defines scope.
            profile = FileSystemPermissionProfile.workspace(
                workspace=Path(binding.workspace),
                denied_read_roots=(
                    Path(path).expanduser() for path in self._config.sandbox.denied_read_roots
                ),
                denied_read_globs=self._config.sandbox.denied_read_globs,
            )

            def read_guard(candidate: Path) -> None:
                if (
                    decide_path_access(
                        candidate, workspace=binding.workspace, profile=profile
                    ).status
                    != "allowed"
                ):
                    raise ContentNotFoundError("Working file is outside the authorized read scope")

            if query.format == "zip":
                def capture_working() -> ArtifactBundle:
                    observed: dict[Path, tuple[int, int, int, int]] = {}

                    def fingerprint(path: Path) -> tuple[int, int, int, int]:
                        value = native_io_path(path).stat(follow_symlinks=False)
                        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

                    def guarded_read(candidate: Path) -> None:
                        read_guard(candidate)
                        observed.setdefault(candidate, fingerprint(candidate))

                    if binding.bundle_mode == "none":
                        guarded_read(binding.entry)
                        bundle = ArtifactBundle(binding.entrypoint, (ArtifactBundleSourceFile(
                            binding.entrypoint, binding.entry_mime,
                            _read_regular_bundle_file(
                                binding.entry, max_bytes=DEFAULT_ARTIFACT_MAX_BYTES,
                            ),
                        ),))
                    else:
                        bundle = binding.bundle(read_guard=guarded_read)
                    member = next((item for item in bundle.files if item.path == selected), None)
                    if member is None or member.mime not in {"text/html", "application/xhtml+xml"}:
                        raise ContentNotFoundError("Working page unavailable")
                    if any(fingerprint(path) != value for path, value in observed.items()):
                        raise ContentExportError(
                            "ZIP_SOURCE_CHANGED", 409, "Working files changed during export",
                        )
                    return bundle

                async def validate_export() -> None:
                    await validate_workspace()
                    if await get_working_files(service, query.document_id) != binding:
                        raise ContentNotFoundError("Working file binding changed")

                return await self._zip_exports.export(capture_working, validate=validate_export)

            bundle = await asyncio.to_thread(binding.bundle, read_guard=read_guard)
            member = next((item for item in bundle.files if item.path == selected), None)
            if member is None or member.mime not in {"text/html", "application/xhtml+xml"}:
                raise ContentNotFoundError("Working page unavailable")
            path = (
                binding.entry
                if selected == binding.entrypoint
                else checked_path(binding.root, selected)
            )
            await validate_workspace()
            return WorkingFileMaterial(
                query.document_id,
                selected,
                binding.workspace,
                path,
                member.mime,
                member.data,
            )
        except (
            ArtifactSessionNotFoundError,
            ArtifactNotFoundError,
            ProjectWorkspaceStateError,
            RpcHandlerError,
            OSError,
            ValueError,
        ) as exc:
            raise ContentNotFoundError("Working file unavailable") from exc
        finally:
            await service.close()

    async def artifact_content(self, query: ArtifactContentQuery) -> ContentMaterial:
        session_id = await self._session_id(query.session_key)
        if session_id is None:
            raise ContentNotFoundError("artifact not found")
        store = ArtifactStore(media_root_from_config(self._config))
        try:
            if query.format == "zip":
                return await self._export_artifact(query.session_key, session_id, query.artifact_id)
            ref, path = await asyncio.to_thread(
                store.resolve_for_download, query.artifact_id, session_id=session_id
            )
            if query.thumbnail:
                thumbnail = await asyncio.to_thread(
                    store.resolve_thumbnail_for_download,
                    query.artifact_id,
                    session_id=session_id,
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
            try:
                document = await service.get_document(query.document_id)
                if document.session_key != query.session_key or document.session_id != session_id:
                    raise ArtifactSessionNotFoundError("artifact document not found")
                revision_id = query.revision_id or document.head_revision_id
                revision = await service.get_revision(revision_id)
                if revision.document_id != document.document_id:
                    raise ArtifactSessionNotFoundError("artifact revision not found")
            finally:
                await service.close()
            if query.format == "zip":
                return await self._export_artifact(
                    query.session_key, session_id, revision.artifact_id,
                    expected_sha256=revision.artifact_sha256, expected_size=revision.byte_size,
                )
            ref, path = await asyncio.to_thread(
                ArtifactStore(media_root_from_config(self._config)).resolve_for_download,
                revision.artifact_id,
                session_id=session_id,
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

    async def _export_artifact(
        self, session_key: str, session_id: str, artifact_id: str, *,
        expected_sha256: str | None = None, expected_size: int | None = None,
    ) -> ContentMaterial:
        store = ArtifactStore(media_root_from_config(self._config))

        async def validate() -> None:
            if await self._session_id(session_key) != session_id:
                raise ContentNotFoundError("Artifact session changed")

        return await self._zip_exports.export(
            lambda: capture_saved_html(
                store, artifact_id, session_id,
                expected_sha256=expected_sha256, expected_size=expected_size,
            ), validate=validate,
        )

    async def attachment_content(self, query: AttachmentContentQuery) -> ContentMaterial:
        session_id = await self._session_id(query.session_key)
        if session_id is None:
            raise ContentNotFoundError("attachment not found")
        if query.message_id is not None:
            from opensquilla.content_reader import (
                ContentMetadataPendingError,
                ContentRangeError,
            )
            from opensquilla.content_reader import (
                ContentNotFoundError as StorageContentNotFoundError,
            )

            storage = get_session_storage(self._session_manager)
            if storage is None:
                raise ContentNotFoundError("attachment source unavailable")
            try:
                ref = await storage.get_legacy_content_ref(
                    session_id, query.message_id, source=query.source, actual_byte_length=True
                )
                if ref.revision != query.revision:
                    raise ContentNotFoundError("attachment source changed")
                attachment = await storage.read_inline_attachment(ref, query.attachment_index)
            except (StorageContentNotFoundError, ContentMetadataPendingError) as exc:
                raise ContentNotFoundError("attachment source unavailable") from exc
            except ContentRangeError as exc:
                raise ContentIntegrityError("attachment data is invalid") from exc
            if attachment["sha256_ref"] != query.sha256:
                raise ContentIntegrityError("attachment integrity check failed")
            # Recheck the owning session after the asynchronous read, so a
            # concurrent reset cannot publish bytes under its replacement.
            if await self._session_id(query.session_key) != session_id:
                raise ContentNotFoundError("attachment session changed")
            return ContentMaterial(None, attachment["mime"], attachment["name"], attachment["data"])
        try:
            path = transcript_material_path(
                media_root_from_config(self._config), session_id, query.sha256
            )
        except ValueError as exc:
            raise ContentNotFoundError("attachment not found") from exc
        native_path = native_io_path(path)
        await asyncio.to_thread(_verify_attachment_material, native_path, query.sha256)
        # The integrity read yields to the event loop just like inline reads;
        # do not publish old material under a concurrently replaced session.
        if await self._session_id(query.session_key) != session_id:
            raise ContentNotFoundError("attachment session changed")
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
        if normalized in IMAGE_ATTACHMENT_MIMES:
            sniffed = sniff_mime_from_bytes(payload)
            return sniffed if sniffed in IMAGE_ATTACHMENT_MIMES else normalized
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
