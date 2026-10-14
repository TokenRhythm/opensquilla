"""Narrow Gateway adapter for generated deliverable adoption."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from opensquilla.artifact_session import ArtifactSessionService
from opensquilla.artifact_session.errors import ArtifactConflictError
from opensquilla.artifact_session.models import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactKind,
    RevisionSource,
)
from opensquilla.artifacts import (
    ArtifactError,
    ArtifactRef,
    ArtifactSource,
    ArtifactStore,
    _read_regular_bundle_file,
    artifact_mime_for_name,
    collect_artifact_bundle,
)
from opensquilla.engine.types import ArtifactEvent
from opensquilla.gateway.workbench_resource_runtime import (
    adopt_generated_deliverable_if_editable,
)
from opensquilla.sandbox.integration import active_file_system_profile
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.tools.types import ToolContext, current_tool_context

ArtifactStateEmitter = Callable[[dict[str, Any]], Awaitable[None]]
log = logging.getLogger(__name__)

_VISUALIZE_WIDGET_MAX_BYTES = 1024 * 1024
_VISUALIZE_WIDGET_TITLE_MAX_CHARS = 128
_VISUALIZE_WIDGET_MAX_EDITS = 32
_VISUALIZE_WIDGET_EDIT_MAX_BYTES = 256 * 1024


@dataclass(slots=True)
class GeneratedArtifactAdopter:
    """Adopt editable turn artifacts without exposing persistence to the engine.

    The instance is bound to one accepted session turn.  It validates every
    public event against that authority before resolving the immutable object
    and asking the ArtifactSession layer to create its canonical Document.
    """

    service: ArtifactSessionService
    store: ArtifactStore
    session_key: str
    session_id: str
    event_emitter: ArtifactStateEmitter | None = None
    workspace: str | None = None
    preview_service: Any = None
    source_paths: dict[str, ArtifactSource] = field(default_factory=dict)
    validate_session: Callable[[Any], Awaitable[None]] | None = None
    _widget_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @staticmethod
    def _normalize_widget_title(title: str) -> str:
        normalized = " ".join(title.split()) if isinstance(title, str) else ""
        if not normalized:
            raise ValueError("show_widget title must be a nonempty short label")
        if len(normalized) > _VISUALIZE_WIDGET_TITLE_MAX_CHARS:
            raise ValueError("show_widget title exceeds the 128 character limit")
        return normalized

    @staticmethod
    def _widget_name(title: str) -> tuple[str, str]:
        """Return the stable store filename and model-facing widget id."""
        slug = re.sub(r"[^A-Za-z0-9_-]+", "-", title).strip("-_")
        slug = (slug or "widget")[:64]
        title_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
        return f"{slug}-{title_hash}.html", f"widget:{title_hash}"

    async def _find_widget_document(self, name: str):
        documents = await self.service.list_documents(
            session_key=self.session_key,
            session_id=self.session_id,
            limit=500,
        )
        for document in documents:
            if document.name != name or document.kind is not ArtifactKind.HTML:
                continue
            try:
                head = await self.service.get_document_head(document.document_id)
                ref = self.store.get_ref(
                    session_id=self.session_id,
                    artifact_id=head.revision.artifact_id,
                )
            except ArtifactError:
                continue
            if ref.source == "show-widget":
                return document
        return None

    @staticmethod
    def _apply_widget_edits(widget_code: str, edits: list[dict[str, str]]) -> str:
        if not isinstance(edits, list) or not edits:
            raise ValueError("edit_widget edits must contain at least one edit")
        if len(edits) > _VISUALIZE_WIDGET_MAX_EDITS:
            raise ValueError(
                f"edit_widget accepts at most {_VISUALIZE_WIDGET_MAX_EDITS} edits"
            )
        updated = widget_code
        for index, edit in enumerate(edits, start=1):
            if not isinstance(edit, dict):
                raise ValueError(f"edit_widget edit {index} must be an object")
            old_string = edit.get("old_string")
            new_string = edit.get("new_string")
            if not isinstance(old_string, str) or not old_string:
                raise ValueError(f"edit_widget edit {index} old_string must be nonempty")
            if not isinstance(new_string, str):
                raise ValueError(f"edit_widget edit {index} new_string must be a string")
            if len(old_string.encode("utf-8")) > _VISUALIZE_WIDGET_EDIT_MAX_BYTES:
                raise ValueError(f"edit_widget edit {index} old_string is too large")
            if len(new_string.encode("utf-8")) > _VISUALIZE_WIDGET_EDIT_MAX_BYTES:
                raise ValueError(f"edit_widget edit {index} new_string is too large")
            matches = updated.count(old_string)
            if matches == 0:
                raise ValueError(
                    f"edit_widget edit {index} old_string was not found; no changes applied"
                )
            if matches != 1:
                raise ValueError(
                    f"edit_widget edit {index} old_string must match exactly once"
                )
            updated = updated.replace(old_string, new_string, 1)
        return updated

    async def working_source_scopes(self) -> list[dict[str, str]]:
        async with self.service.repository._read_transaction("workspace.preview.scopes") as conn:
            cursor = await conn.execute(
                "SELECT workspace, source_path, bundle_mode, bundle_root "
                "FROM artifact_working_sources WHERE session_key=? AND session_id=?",
                (self.session_key, self.session_id),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def publish_widget(
        self,
        context: ToolContext,
        *,
        title: str,
        widget_code: str,
        loading_messages: list[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a ``generative-frame`` widget as an inline Visualize receipt.

        ``show_widget`` is a model-facing convenience API.  The browser still
        consumes an immutable Artifact document so reconnects, session changes,
        and remote clients do not depend on an in-memory tool registry or a
        browser-side streamed tool call.

        Same-title calls update one durable Document and append an immutable
        ArtifactSession revision.  The stable filename is an internal
        identity, while ``widgetId`` is the host-facing registry key.
        """
        if (
            not isinstance(context, ToolContext)
            or current_tool_context.get() is not context
            or not context.is_owner
            or context.guest_safe
            or context.session_key != self.session_key
            or context.session_id != self.session_id
        ):
            raise ValueError("show_widget context no longer matches the admitted session")

        normalized_title = self._normalize_widget_title(title)
        if not isinstance(widget_code, str) or not widget_code.strip():
            raise ValueError("show_widget widget_code must be a nonempty HTML fragment or SVG")
        payload = widget_code.encode("utf-8")
        if len(payload) > _VISUALIZE_WIDGET_MAX_BYTES:
            raise ValueError("show_widget widget_code exceeds the 1 MiB limit")

        from opensquilla.gateway.visualize_html import validate_visualize_html

        # This catches truncation, NULs, Markdown fences and malformed raw text
        # before an immutable revision is created.  It deliberately does not
        # claim that browser scripts/layout or semantic calculations succeeded.
        await asyncio.to_thread(validate_visualize_html, payload)

        name, widget_id = self._widget_name(normalized_title)
        async with self._widget_lock:
            ref = await asyncio.to_thread(
                self.store.publish_bytes,
                payload,
                session_id=self.session_id,
                session_key=self.session_key,
                name=name,
                mime="text/html",
                source="show-widget",
                visibility="internal",
            )
            existing = await self._find_widget_document(name)
            if existing is None:
                commit = await self.service.create_document(
                    session_key=self.session_key,
                    session_id=self.session_id,
                    name=name,
                    kind=ArtifactKind.HTML,
                    initial_artifact=ArtifactBlobRef(
                        artifact_id=ref.id,
                        sha256=ref.sha256,
                        filename=ref.name,
                        media_type=ref.mime,
                        byte_size=ref.size,
                    ),
                    actor=Actor(kind=ActorKind.SYSTEM, actor_id="visualize"),
                    validate_session=self.validate_session,
                )
            else:
                head = await self.service.get_document_head(existing.document_id)
                commit = await self.service.commit_revision(
                    document_id=existing.document_id,
                    expected_head_revision_id=head.document.head_revision_id,
                    expected_state_revision=head.document.state_revision,
                    artifact=ArtifactBlobRef(
                        artifact_id=ref.id,
                        sha256=ref.sha256,
                        filename=ref.name,
                        media_type=ref.mime,
                        byte_size=ref.size,
                    ),
                    actor=Actor(kind=ActorKind.SYSTEM, actor_id="visualize"),
                    source=RevisionSource.AGENT,
                )
        document_id = commit.document.document_id
        revision_id = commit.revision.revision_id
        result: dict[str, Any] = {
            "ok": True,
            "title": normalized_title,
            "widgetId": widget_id,
            "action": "show",
            "kind": (
                "svg"
                if widget_code.lstrip().casefold().startswith("<svg")
                else "html"
            ),
            "previewStatus": "inline-ready",
            "presentation": "inline",
            "documentId": document_id,
            "revisionId": revision_id,
            "artifactId": ref.id,
            # Keep the model-facing title as the chat label.  The content
            # store's collision-safe filename remains available separately.
            "name": normalized_title,
            "artifactName": ref.name,
            "mime": ref.mime,
            "sha256": ref.sha256,
            "size": ref.size,
            "marker": f"visualize:{document_id}:{revision_id}",
            "renderStatus": "not-observed",
            "version": 1,
        }
        result["version"] = commit.document.generation
        if loading_messages is not None:
            result["loadingMessages"] = list(loading_messages)
        return result

    async def edit_widget(
        self,
        context: ToolContext,
        *,
        title: str,
        edits: list[dict[str, str]],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Apply upstream-compatible exact replacements to a persisted widget.

        The latest ArtifactSession head is the source of truth.  The edit is
        validated fully before ``commit_revision`` performs a head/state CAS,
        so a stale model call cannot overwrite a newer widget version.
        """
        if (
            not isinstance(context, ToolContext)
            or current_tool_context.get() is not context
            or not context.is_owner
            or context.guest_safe
            or context.session_key != self.session_key
            or context.session_id != self.session_id
        ):
            raise ValueError("edit_widget context no longer matches the admitted session")
        normalized_title = self._normalize_widget_title(title)
        name, widget_id = self._widget_name(normalized_title)
        async with self._widget_lock:
            document = await self._find_widget_document(name)
            if document is None:
                raise ValueError(
                    f'No widget titled "{normalized_title}". '
                    "Use show_widget to create it."
                )
            head = await self.service.get_document_head(document.document_id)
            version = head.document.generation
            if expected_version is not None:
                if type(expected_version) is not int or expected_version < 1:
                    raise ValueError("edit_widget expected_version must be a positive integer")
                if expected_version != version:
                    raise ArtifactConflictError(
                        f"widget version conflict (expected {expected_version}, current {version})"
                    )
            _, payload, _ = await asyncio.to_thread(
                self.store.read_verified_material,
                head.revision.artifact_id,
                session_id=self.session_id,
                max_bytes=_VISUALIZE_WIDGET_MAX_BYTES,
            )
            try:
                current_code = payload.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("widget artifact is not valid UTF-8") from exc
            updated_code = self._apply_widget_edits(current_code, edits)
            updated_payload = updated_code.encode("utf-8")
            if len(updated_payload) > _VISUALIZE_WIDGET_MAX_BYTES:
                raise ValueError("edit_widget widget_code exceeds the 1 MiB limit")
            from opensquilla.gateway.visualize_html import validate_visualize_html

            await asyncio.to_thread(validate_visualize_html, updated_payload)
            ref = await asyncio.to_thread(
                self.store.publish_bytes,
                updated_payload,
                session_id=self.session_id,
                session_key=self.session_key,
                name=head.revision.filename,
                mime=head.revision.media_type,
                source="show-widget",
                visibility="internal",
            )
            commit = await self.service.commit_revision(
                document_id=document.document_id,
                expected_head_revision_id=head.document.head_revision_id,
                expected_state_revision=head.document.state_revision,
                artifact=ArtifactBlobRef(
                    artifact_id=ref.id,
                    sha256=ref.sha256,
                    filename=ref.name,
                    media_type=ref.mime,
                    byte_size=ref.size,
                ),
                actor=Actor(kind=ActorKind.SYSTEM, actor_id="visualize-edit"),
                source=RevisionSource.AGENT,
            )
        return {
            "ok": True,
            "title": normalized_title,
            "widgetId": widget_id,
            "action": "edit",
            "applied": len(edits),
            "version": commit.document.generation,
            "previewStatus": "inline-ready",
            "presentation": "inline",
            "documentId": commit.document.document_id,
            "revisionId": commit.revision.revision_id,
            "artifactId": ref.id,
            "name": normalized_title,
            "artifactName": ref.name,
            "mime": ref.mime,
            "sha256": ref.sha256,
            "size": ref.size,
            "marker": f"visualize:{commit.document.document_id}:{commit.revision.revision_id}",
            "renderStatus": "not-observed",
        }

    async def open_workspace_preview(
        self, context: ToolContext, *, path: str, bundle: str | None = None,
        bundle_root: str | None = None,
        presentation: str = "workbench",
    ) -> dict[str, Any]:
        """Register an HTML source as an internal Document without publishing it."""
        if self.workspace is None:
            raise ValueError("WORKSPACE_PREVIEW_UNAVAILABLE: no workspace is bound")
        if (
            not isinstance(context, ToolContext)
            or current_tool_context.get() is not context
            or not context.is_owner
            or context.guest_safe
            or context.session_key != self.session_key
            or context.session_id != self.session_id
            or context.workspace_dir != self.workspace
        ):
            raise ValueError("workspace preview context no longer matches the admitted session")
        from opensquilla.artifact_session.working_files import checked_path, get_working_files
        from opensquilla.tools.builtin.filesystem import _cross_session_read_block

        root = checked_path(Path(self.workspace), ".")
        requested_path = Path(path)
        if requested_path.is_absolute():
            try:
                source_path = requested_path.relative_to(root).as_posix()
            except ValueError as exc:
                raise ValueError(
                    "workspace preview path must be inside the active workspace"
                ) from exc
        else:
            source_path = requested_path.as_posix()
        entry = checked_path(root, source_path)
        profile = active_file_system_profile(root)

        def read_guard(candidate: Path) -> None:
            checked_path(root, candidate.relative_to(root).as_posix())
            decision = decide_path_access(candidate, workspace=root, profile=profile)
            if decision.status != "allowed":
                raise ValueError("workspace preview resource is outside the authorized read scope")
            if _cross_session_read_block("open_workspace_preview", candidate, str(candidate)):
                raise ValueError("workspace preview cannot read another session's private material")

        read_guard(entry)
        if not entry.is_file():
            raise ValueError("workspace preview entrypoint is unavailable")
        if entry.suffix.casefold() not in {".html", ".htm", ".xhtml"}:
            raise ValueError("workspace preview entrypoint must be HTML")
        source_path = entry.relative_to(root).as_posix()
        if presentation not in {"workbench", "inline"}:
            raise ValueError("invalid workspace preview presentation")
        if presentation == "inline":
            if bundle not in {None, "none"} or bundle_root is not None:
                raise ValueError(
                    "inline preview requires a self-contained HTML file; use bundle=none"
                )
            if entry.suffix.casefold() == ".xhtml":
                raise ValueError("inline preview requires .html or .htm")
            # Read one bounded, no-follow snapshot. Never return a live working
            # head: an old chat must keep showing the exact bytes delivered then.
            payload = await asyncio.to_thread(
                _read_regular_bundle_file, entry, max_bytes=1024 * 1024
            )
            if len(entry.name) > 250:
                raise ValueError(
                    "inline preview requires a name <=250 characters"
                )
            from opensquilla.gateway.visualize_html import validate_visualize_html

            await asyncio.to_thread(validate_visualize_html, payload)
            ref = await asyncio.to_thread(
                self.store.publish_bytes, payload,
                session_id=self.session_id, session_key=self.session_key,
                name=entry.name, mime="text/html",
                source="workspace-preview", visibility="internal",
            )
            commit = await self.service.create_document(
                session_key=self.session_key, session_id=self.session_id,
                name=entry.name, kind=ArtifactKind.HTML,
                initial_artifact=ArtifactBlobRef(
                    artifact_id=ref.id, sha256=ref.sha256, filename=ref.name,
                    media_type=ref.mime, byte_size=ref.size,
                ),
                actor=Actor(kind=ActorKind.SYSTEM, actor_id="visualize"),
                validate_session=self.validate_session,
            )
            scope = {"workspace": str(root), "source_path": source_path,
                     "bundle_mode": "none", "bundle_root": ""}
            if scope not in context.workspace_preview_scopes:
                context.workspace_preview_scopes.append(scope)
            document_id = commit.document.document_id
            revision_id = commit.revision.revision_id
            return {
                "previewStatus": "inline-ready", "presentation": "inline",
                "documentId": document_id, "revisionId": revision_id,
                "artifactId": ref.id, "name": ref.name, "mime": ref.mime,
                "sha256": ref.sha256, "size": ref.size,
                "marker": f"visualize:{document_id}:{revision_id}",
                "renderStatus": "not-observed",
            }
        async with self.service.repository._read_transaction("workspace.preview.lookup") as conn:
            if self.validate_session is not None:
                await self.validate_session(conn)
            cursor = await conn.execute(
                "SELECT document_id, bundle_mode, bundle_root FROM artifact_working_sources "
                "WHERE session_key=? AND session_id=? AND workspace=? AND source_path=?",
                (self.session_key, self.session_id, str(root), source_path),
            )
            existing_row = await cursor.fetchone()
        mode = bundle if bundle is not None else (
            str(existing_row[1]) if existing_row is not None else "auto"
        )
        if mode not in {"auto", "directory", "none"}:
            raise ValueError("invalid workspace preview collection mode")
        source_root = None
        if mode == "directory":
            if bundle_root is None and existing_row is not None:
                bundle_root = str(existing_row[2] or "")
            if not bundle_root:
                raise ValueError("directory preview requires bundle_root")
            source_root = checked_path(root, bundle_root)
            if source_root == root or not entry.is_relative_to(source_root):
                raise ValueError(
                    "bundle_root must be a dedicated subdirectory containing the entrypoint"
                )
            read_guard(source_root)
            if not source_root.is_dir():
                raise ValueError("bundle_root must be a directory")
            bundle_root = source_root.relative_to(root).as_posix()
        else:
            if bundle_root is not None:
                raise ValueError("bundle_root is only valid with directory mode")
        if existing_row is not None:
            if bundle is not None or bundle_root is not None:
                recorded_root = str(existing_row[2] or "")
                if (str(existing_row[1]), recorded_root) != (mode, bundle_root or ""):
                    raise ValueError("workspace preview collection scope is already registered")
            commit = await self.service.get_document_head(str(existing_row[0]))
            created = False
        else:
            entry_mime = artifact_mime_for_name(entry)
            collected = await asyncio.to_thread(
                collect_artifact_bundle, entry, workspace_root=root, mode=mode,
                bundle_root=source_root, entry_mime=entry_mime, read_guard=read_guard,
            )
            artifact_args: dict[str, Any] = {
                "session_id": self.session_id,
                "session_key": self.session_key,
                "name": entry.name,
                "mime": entry_mime,
                "source": "workspace-preview",
                "visibility": "internal",
            }
            if collected is None:
                read_guard(entry)
                payload = await asyncio.to_thread(_read_regular_bundle_file, entry)
                ref = await asyncio.to_thread(self.store.publish_bytes, payload, **artifact_args)
            else:
                ref = await asyncio.to_thread(self.store.publish_bundle, collected, **artifact_args)
            working_root = source_root if source_root is not None else entry.parent
            commit, created = await self.service.register_working_source(
                session_key=self.session_key,
                session_id=self.session_id,
                name=entry.name,
                initial_artifact=ArtifactBlobRef(
                    artifact_id=ref.id, sha256=ref.sha256, filename=ref.name,
                    media_type=ref.mime, byte_size=ref.size,
                ),
                actor=Actor(kind=ActorKind.SYSTEM, actor_id="workspace-preview"),
                working_source={
                    "workspace": str(root),
                    "source_path": source_path,
                    "relative_root": working_root.relative_to(root).as_posix(),
                    "entrypoint": collected.entrypoint if collected else entry.name,
                    "bundle_mode": mode,
                    "bundle_root": bundle_root or "",
                },
                validate_session=self.validate_session,
            )
        source = {
            "workspace": str(root),
            "source_path": source_path,
            "bundle_mode": mode,
            "bundle_root": bundle_root or "",
        }
        if source not in context.workspace_preview_scopes:
            context.workspace_preview_scopes.append(source)
        preview_status = "registered"
        if self.preview_service is not None:
            binding = await get_working_files(self.service, commit.document.document_id)
            if binding is not None:
                self.preview_service.register_working_files(
                    session_id=self.session_id,
                    artifact_id=commit.revision.artifact_id,
                    binding=binding,
                )
                preview_status = "ready"
        if created and self.event_emitter is not None:
            try:
                audit = await self.service.latest_audit_event(commit.document.document_id)
                if audit is not None:
                    await self.event_emitter({
                        "artifactEventSeq": audit.sequence,
                        "documentId": commit.document.document_id,
                        "revisionId": commit.revision.revision_id,
                        "changeSetId": None,
                        "action": "document.created",
                    })
            except Exception:
                # Registration is durable; a disconnected UI can discover it
                # from the resource list when it reconnects.
                log.warning("Workspace preview notification failed", exc_info=True)
        return {
            "resourceId": f"document:{commit.document.document_id}",
            "entrypoint": str(entry),
            "workspace": str(root),
            "bundleMode": mode,
            "bundleRoot": source.get("bundle_root") or None,
            "created": created,
            "previewStatus": preview_status,
            "documentId": commit.document.document_id,
            "revisionId": commit.revision.revision_id,
            "open": {"resourceId": f"document:{commit.document.document_id}"},
        }

    def with_source_paths(
        self, source_paths: dict[str, ArtifactSource],
    ) -> GeneratedArtifactAdopter:
        """Bind a runtime turn's source records without mutating its caller."""
        return replace(self, source_paths=source_paths)

    async def artifact_ids_for_source(self, source: ArtifactSource) -> set[str]:
        if self.workspace is None:
            return set()
        workspace = str(Path(self.workspace).resolve())
        try:
            relative = Path(source.path).relative_to(workspace).as_posix()
        except ValueError:
            return set()
        async with self.service.repository._read_transaction("generated.source_artifacts") as conn:
            cursor = await conn.execute(
                "SELECT revision.artifact_id FROM artifact_working_sources AS source "
                "JOIN artifact_revisions AS revision USING(document_id) "
                "WHERE source.session_key=? AND source.session_id=? "
                "AND source.workspace=? AND source.source_path=?",
                (self.session_key, self.session_id, workspace, relative),
            )
            return {str(row[0]) for row in await cursor.fetchall()}

    async def __call__(self, event: ArtifactEvent) -> None:
        if not isinstance(event, ArtifactEvent):
            raise TypeError("generated artifact adopter requires an ArtifactEvent")
        artifact_id = event.id.strip()
        if not artifact_id:
            raise ValueError("generated artifact event is missing its artifact id")
        if event.session_id and event.session_id != self.session_id:
            raise ValueError("generated artifact event belongs to another session")
        if event.session_key and event.session_key != self.session_key:
            raise ValueError("generated artifact event belongs to another session key")

        ref = await asyncio.to_thread(
            self.store.get_ref,
            session_id=self.session_id,
            artifact_id=artifact_id,
        )
        if ref.session_key != self.session_key:
            raise ValueError("generated artifact metadata belongs to another session key")
        for event_value, stored_value, field_name in (
            (event.sha256, ref.sha256, "sha256"),
            (event.name, ref.name, "name"),
            (event.mime, ref.mime, "mime"),
        ):
            if event_value and event_value != stored_value:
                raise ValueError(f"generated artifact {field_name} changed before adoption")
        if event.size and event.size != ref.size:
            raise ValueError("generated artifact size changed before adoption")

        async with self.service.repository._read_transaction("generated.publication") as conn:
            cursor = await conn.execute(
                "SELECT publication_id FROM document_publications "
                "WHERE session_id=? AND session_key=? AND deliverable_artifact_id=? LIMIT 1",
                (self.session_id, self.session_key, ref.id),
            )
            already_published = await cursor.fetchone() is not None
        source = None
        candidate = self.source_paths.get(event.publication_id)
        if candidate is not None and candidate.artifact_id != ref.id:
            raise ValueError("generated publication belongs to another artifact")
        if self.workspace is not None and candidate is not None:
            candidate_root = Path(self.workspace) / "artifacts" / ".office-candidates"
            if Path(candidate.path).is_relative_to(candidate_root):
                # Candidates are never adopted by publication, including queue recovery
                # where no runtime adopter object survived. Only turn completion commits.
                return
        if self.workspace is not None and candidate is not None:
            from opensquilla.artifact_session.working_files import prepare_working_source

            try:
                source = await asyncio.to_thread(
                    prepare_working_source, self.store, ref, self.workspace, candidate
                )
            except (ValueError, OSError):
                source = None
        if await self._adopt_working_publication(
            ref, source, event.publication_id, notify=not already_published,
        ):
            return
        if already_published:
            return

        adopted = await adopt_generated_deliverable_if_editable(
            service=self.service,
            store=self.store,
            session_key=self.session_key,
            session_id=self.session_id,
            ref=ref,
            working_source=source,
            publication_id=event.publication_id,
        )
        if adopted is None:
            return
        document, revision, _binding, created = adopted
        if source is not None:
            await self._adopt_working_publication(
                ref, source, event.publication_id, notify=not created,
            )
            head = await self.service.get_document_head(document.document_id)
            document, revision = head.document, head.revision
        if self.workspace is not None:
            from opensquilla.artifact_session.working_files import ensure_working_files

            working = await ensure_working_files(
                self.service,
                self.store,
                document_id=document.document_id,
                session_key=self.session_key,
                session_id=self.session_id,
                workspace=self.workspace,
            )
            if self.preview_service is not None:
                self.preview_service.register_working_files(
                    session_id=self.session_id,
                    artifact_id=revision.artifact_id,
                    binding=working,
                )
        if not created or self.event_emitter is None:
            return
        latest = await self.service.latest_audit_event(document.document_id)
        if latest is None:
            return
        await self.event_emitter(
            {
                "artifactEventSeq": latest.sequence,
                "documentId": document.document_id,
                "revisionId": revision.revision_id,
                "changeSetId": None,
                "action": "document.created",
            }
        )

    async def _adopt_working_publication(
        self, ref: ArtifactRef, source: dict[str, str] | None, publication_id: str,
        *, notify: bool = True,
    ) -> bool:
        if self.workspace is None:
            return False
        candidate = self.source_paths.get(publication_id)
        if not publication_id or (candidate is not None and not Path(candidate.path).is_absolute()):
            return False
        from opensquilla.artifact_session.working_files import (
            get_working_files,
            save_working_version,
        )

        workspace = str(Path(self.workspace).resolve())
        async with self.service.repository._read_transaction("generated.working_source") as conn:
            cursor = await conn.execute(
                "SELECT working.* FROM artifact_working_files AS working "
                "JOIN artifact_documents AS document USING(document_id) "
                "WHERE document.session_id=? AND document.session_key=? AND working.workspace=?",
                (self.session_id, self.session_key, workspace),
            )
            rows = await cursor.fetchall()
        matches = []
        for row in rows:
            binding = await get_working_files(self.service, row["document_id"])
            if binding is None:
                continue
            if candidate is not None and candidate.path == str(binding.entry):
                matches.append(binding)
            elif candidate is None:
                identity = f"{binding.document_id}\0{publication_id}".encode()
                key = f"working-publish:{hashlib.sha256(identity).hexdigest()}"
                async with self.service.repository._read_transaction(
                    "generated.publication_replay",
                ) as conn:
                    cursor = await conn.execute(
                        "SELECT 1 FROM artifact_audit_events WHERE event_id=? AND document_id=?",
                        (key, binding.document_id),
                    )
                    if await cursor.fetchone() is not None:
                        matches.append(binding)
        if len(matches) != 1:
            return False
        binding = matches[0]
        from opensquilla.office_format import office_format

        if office_format(binding.entrypoint, binding.entry_mime):
            # This download is not a committed Office version or publication receipt.
            return True
        saved = await save_working_version(
            self.service,
            self.store,
            document_id=binding.document_id,
            session_key=self.session_key,
            session_id=self.session_id,
            actor_id="generated-deliverable",
            published_ref=ref,
            working_source=source,
            publication_id=publication_id,
        )
        head = await self.service.get_document_head(binding.document_id)
        updated = await get_working_files(self.service, binding.document_id)
        if self.preview_service is not None and updated is not None:
            self.preview_service.register_working_files(
                session_id=self.session_id,
                artifact_id=head.revision.artifact_id,
                binding=updated,
            )
        latest = await self.service.latest_audit_event(binding.document_id)
        if (notify or saved is not None) and self.event_emitter is not None and latest is not None:
            await self.event_emitter(
                {
                    "artifactEventSeq": latest.sequence,
                    "documentId": binding.document_id,
                    "revisionId": head.revision.revision_id,
                    "changeSetId": None,
                    "action": "document.published",
                }
            )
        return True


__all__ = ["GeneratedArtifactAdopter"]
