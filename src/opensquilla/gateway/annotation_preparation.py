"""Prepare an annotation baseline only on an explicit user editing action.

Discovery and chat admission use the read-only helpers below. They must never
advance the baseline under an existing selection.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from opensquilla.artifact_session import (
    Actor,
    ActorKind,
    ArtifactBlobRef,
    ArtifactConflictError,
    ArtifactKind,
    ArtifactSessionService,
    ArtifactValidationError,
)
from opensquilla.artifact_session.errors import ArtifactSessionError
from opensquilla.artifact_session.models import RevisionSource
from opensquilla.artifact_session.working_files import (
    WorkingFiles,
    _load_office_version_bundle,
    _lock,
    _save_source_binding,
    _working_bundle_digest,
    checked_path,
    get_working_files,
    load_version_bundle,
)
from opensquilla.artifacts import (
    ArtifactBundle,
    ArtifactBundleSourceFile,
    ArtifactStore,
    collect_artifact_bundle,
)
from opensquilla.gateway.session_services import get_session_storage
from opensquilla.html_format import is_html_preview_path, validate_html
from opensquilla.office_format import OFFICE_MIMES, office_format, validate_office_bytes
from opensquilla.paths import media_root_from_config, native_io_path
from opensquilla.sandbox.path_validation import decide_path_access
from opensquilla.session.keys import canonicalize_session_key


def annotation_format(name: str, mime: str = "") -> str | None:
    modern = office_format(name, mime)
    if modern:
        return modern
    return "html" if Path(name).suffix.lower() in {".html", ".htm", ".xhtml"} else None


def _validate_bundle(bundle: ArtifactBundle, modern: str) -> None:
    entry = next(item for item in bundle.files if item.path == bundle.entrypoint)
    if modern == "html":
        validate_html(entry.data.decode("utf-8-sig"))
    else:
        if len(bundle.files) != 1:
            raise ArtifactValidationError("Office annotation source must be one package")
        validate_office_bytes(entry.data, modern)


def _source_guard(workspace: Any):
    from opensquilla.gateway.workspace_files import _readable_file

    def guard(path: Path) -> None:
        checked_path(workspace.root, path.relative_to(workspace.root).as_posix())
        if decide_path_access(
            path, workspace=workspace.root, profile=workspace.profile,
        ).status != "allowed":
            raise ArtifactValidationError("Annotation source is outside the authorized read scope")
        # Directory collectors authorize their root before visiting each file.
        if native_io_path(path).is_dir():
            return
        _readable_file(workspace, str(path))

    return guard


async def find_document_target(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    session_key: str,
    session_id: str,
    workspace: Any,
    path: Path,
) -> tuple[dict[str, str] | None, bool]:
    """Match exact bindings first, then authorized HTML bundle members.

    Never infer membership from directory containment alone. Shared assets and
    overlapping bundles cannot silently choose a document.
    """
    async with service.repository._read_transaction("annotation.target") as conn:
        cursor = await conn.execute(
            "SELECT document.document_id FROM artifact_documents AS document "
            "JOIN artifact_working_files AS working USING(document_id) "
            "WHERE document.session_key=? AND document.session_id=? AND working.workspace=?",
            (session_key, session_id, str(workspace.root)),
        )
        rows = await cursor.fetchall()
    exact, members, bindings = [], [], []
    for row in rows:
        binding = await get_working_files(service, row["document_id"])
        if binding is None:
            continue
        bindings.append(binding)
        target = {"documentId": binding.document_id}
        if binding.entry == path:
            exact.append(target)
    if exact:
        return (exact[0] if len(exact) == 1 else None), len(exact) > 1
    for binding in bindings:
        target = {"documentId": binding.document_id}
        if not is_html_preview_path(path.name) or not path.is_relative_to(binding.root):
            continue
        page = path.relative_to(binding.root).as_posix()
        head = await service.get_document_head(binding.document_id)
        manifest = await asyncio.to_thread(
            store.describe_preview_bundle, head.revision.artifact_id, session_id=session_id
        )
        known_member = manifest is not None and any(
            item.path == page and item.mime in {"text/html", "application/xhtml+xml"}
            for item in manifest.files
        )
        if not known_member and annotation_format(binding.entrypoint, binding.entry_mime) == "html":
            # A new child can already be visible in a live working preview,
            # before the next immutable version has been recorded.
            bundle = await asyncio.to_thread(binding.bundle, read_guard=_source_guard(workspace))
            known_member = any(
                item.path == page and item.mime in {"text/html", "application/xhtml+xml"}
                for item in bundle.files
            )
        if known_member:
            _source_guard(workspace)(path)
            members.append({**target, "pagePath": page})
    return (members[0] if len(members) == 1 else None), len(members) > 1


async def discover_workspace_annotations(
    config: Any, manager: Any, session_key: str, workspace: Any, files: list[dict[str, Any]]
) -> None:
    """Enrich a read-only file inventory; do not register or refresh anything."""
    for item in files:
        supported = annotation_format(item["name"], item["mime"]) is not None
        item["annotationCapability"] = {
            "supported": supported,
            **({} if supported else {"reasonCode": "format_unsupported"}),
        }
    storage = get_session_storage(manager)
    if storage is None or not any(item["annotationCapability"]["supported"] for item in files):
        return
    key = canonicalize_session_key(session_key)
    session = await storage.get_session(key)
    if session is None:
        return
    service = await ArtifactSessionService.from_session_storage(storage)
    store = ArtifactStore(media_root_from_config(config))
    try:
        for item in files:
            if not item["annotationCapability"]["supported"]:
                continue
            try:
                target, ambiguous = await find_document_target(
                    service,
                    store,
                    session_key=key,
                    session_id=session.session_id,
                    workspace=workspace,
                    path=workspace.root / item["path"],
                )
            except (ArtifactSessionError, OSError, ValueError):
                # A broken associated preview must not hide the ordinary file.
                item["annotationCapability"] = {
                    "supported": False, "reasonCode": "document_target_unavailable",
                }
                continue
            if ambiguous:
                item["annotationCapability"] = {
                    "supported": False,
                    "reasonCode": "document_target_ambiguous",
                }
            elif target is not None:
                item["documentTarget"] = target
    finally:
        await service.close()


async def annotation_authority(ctx: Any, session_key: str, session_id: str):
    from opensquilla.agents.scope import resolve_agent_workspace_dir
    from opensquilla.gateway.project_workspace_runtime import authoritative_project_run_context
    from opensquilla.gateway.workspace_files import WorkspaceFiles
    from opensquilla.session.keys import parse_agent_id
    from opensquilla.session.storage import _verify_project_workspace_guard

    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise ArtifactValidationError("Annotation requires session storage")
    session = await storage.get_session(session_key)
    if session is None or session.session_id != session_id:
        raise ArtifactConflictError("Annotation session changed")
    files = WorkspaceFiles(ctx.config, ctx.session_manager)
    workspace = await files.workspace(session_key)
    default = resolve_agent_workspace_dir(parse_agent_id(session_key), ctx.config)
    _, project_guard = await authoritative_project_run_context(
        storage=storage,
        session_manager=ctx.session_manager,
        session=session,
        config=ctx.config,
        default_workspace=str(default) if default else None,
        include_user_grants=False,
    )

    async def validate(conn: Any) -> None:
        cursor = await conn.execute(
            "SELECT session_id, epoch, execution_workspace FROM sessions WHERE session_key=?",
            (session_key,),
        )
        row = await cursor.fetchone()
        if (
            row is None
            or row["session_id"] != session_id
            or row["epoch"] != session.epoch
            or (json.loads(row["execution_workspace"]) if row["execution_workspace"] else None)
            != session.execution_workspace
        ):
            raise ArtifactConflictError("Annotation session or workspace changed")
        await _verify_project_workspace_guard(
            conn,
            session_node=session,
            entry_session_key=session_key,
            workspace_guard=project_guard,
        )

    return files, workspace, validate


async def register_annotation_source(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    session_key: str,
    session_id: str,
    workspace: Any,
    path: Path,
    validate: Any,
) -> str:
    """Capture the original path, without writing or copying back to that path."""
    from opensquilla.gateway.workspace_files import _read_file, _workspace_file_mime

    mime = _workspace_file_mime(path.name)
    modern = annotation_format(path.name, mime)
    if modern is None:
        raise ArtifactValidationError("File format does not support annotations")
    guard = _source_guard(workspace)
    mode = "auto" if modern == "html" else "none"
    relative = path.relative_to(workspace.root).as_posix()
    if modern == "html":
        bundle = await asyncio.to_thread(
            collect_artifact_bundle,
            path,
            workspace_root=workspace.root,
            mode=mode,
            entry_mime=mime,
            read_guard=guard,
        )
    else:
        _, payload = await asyncio.to_thread(_read_file, workspace, relative)
        bundle = ArtifactBundle(
            entrypoint=path.name,
            files=(
                ArtifactBundleSourceFile(path=path.name, mime=OFFICE_MIMES[modern], data=payload),
            ),
        )
    if bundle is None:
        raise ArtifactValidationError("HTML source is unavailable")
    await asyncio.to_thread(_validate_bundle, bundle, modern)
    source = {
        "workspace": str(workspace.root),
        "source_path": relative,
        "relative_root": path.parent.relative_to(workspace.root).as_posix(),
        "entrypoint": bundle.entrypoint,
        "bundle_mode": mode,
        "bundle_root": "",
    }
    captured = _working_bundle_digest(bundle)
    binding = WorkingFiles(document_id="", base_revision_id="", entry_mime=mime, **source)
    ref = await asyncio.to_thread(
        store.publish_bundle,
        bundle,
        session_key=session_key,
        session_id=session_id,
        name=path.name,
        mime=mime,
        source="annotation-source",
        visibility="internal",
    )

    async def validate_capture(conn: Any) -> None:
        await validate(conn)
        if (
            await asyncio.to_thread(
                lambda: _working_bundle_digest(binding.bundle(read_guard=guard))
            )
            != captured
        ):
            raise ArtifactConflictError("Annotation source changed during preparation")

    commit, _ = await service.register_working_source(
        session_key=session_key,
        session_id=session_id,
        name=path.name,
        initial_artifact=ArtifactBlobRef(
            artifact_id=ref.id,
            sha256=ref.sha256,
            filename=ref.name,
            media_type=ref.mime,
            byte_size=ref.size,
        ),
        actor=Actor(kind=ActorKind.USER, actor_id="annotation-prepare"),
        working_source=source,
        validate_session=validate_capture,
        kind={
            "html": ArtifactKind.HTML,
            "docx": ArtifactKind.DOCUMENT,
            "xlsx": ArtifactKind.SPREADSHEET,
            "pptx": ArtifactKind.PRESENTATION,
        }[modern],
    )
    return commit.document.document_id


async def refresh_annotation_baseline(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    document_id: str,
    session_key: str,
    session_id: str,
    workspace: Any,
    validate: Any,
) -> str | None:
    """Import an external edit as MANUAL; never restore bytes onto the source."""
    async with _lock(document_id):
        head = await service.get_document_head(document_id)
        if head.document.session_key != session_key or head.document.session_id != session_id:
            raise ArtifactValidationError("Annotation belongs to another session")
        binding = await get_working_files(service, document_id)
        if binding is None or binding.workspace != str(workspace.root):
            raise ArtifactConflictError("Annotation working source is unavailable")
        if binding.base_revision_id != head.revision.revision_id:
            raise ArtifactConflictError("Annotation working baseline changed")
        modern = annotation_format(head.document.name, head.revision.media_type)
        if modern is None:
            raise ArtifactValidationError("File format does not support annotations")
        guard = _source_guard(workspace)
        bundle = await asyncio.to_thread(binding.bundle, read_guard=guard)
        await asyncio.to_thread(_validate_bundle, bundle, modern)
        previous = await asyncio.to_thread(
            load_version_bundle if modern == "html" else _load_office_version_bundle,
            store,
            head.revision.artifact_id,
            session_id,
        )
        digest = _working_bundle_digest(bundle)
        if digest == _working_bundle_digest(previous):
            return None
        ref = await asyncio.to_thread(
            store.publish_bundle,
            bundle,
            session_key=session_key,
            session_id=session_id,
            name=head.document.name,
            mime=head.revision.media_type,
            source="annotation-source",
            visibility="internal",
        )
        async with service.repository._transaction("annotation.baseline") as conn:
            await validate(conn)
            if (
                await asyncio.to_thread(
                    lambda: _working_bundle_digest(binding.bundle(read_guard=guard))
                )
                != digest
            ):
                raise ArtifactConflictError("Annotation source changed during preparation")
            result = await service.repository._commit_revision_on_conn(
                conn,
                document_id=document_id,
                expected_head_revision_id=head.revision.revision_id,
                expected_state_revision=head.document.state_revision,
                artifact=ArtifactBlobRef(
                    artifact_id=ref.id,
                    sha256=ref.sha256,
                    filename=ref.name,
                    media_type=ref.mime,
                    byte_size=ref.size,
                ),
                actor=Actor(kind=ActorKind.USER, actor_id="annotation-prepare"),
                source=RevisionSource.MANUAL,
            )
            updated = replace(binding, base_revision_id=result.revision.revision_id)
            if binding.source_path is not None:
                await _save_source_binding(conn, updated)
            else:
                await conn.execute(
                    "UPDATE artifact_working_files SET base_revision_id=? WHERE document_id=?",
                    (result.revision.revision_id, document_id),
                )
        return head.revision.revision_id


async def validate_annotation_source(
    service: ArtifactSessionService,
    store: ArtifactStore,
    *,
    document_id: str,
    session_key: str,
    session_id: str,
    revision_id: str,
    sha256: str,
    workspace: Any,
) -> None:
    """Read-only selection fence for admission and actual execution."""
    head = await service.get_document_head(document_id)
    if head.document.session_key != session_key or head.document.session_id != session_id:
        raise ArtifactValidationError("Annotation belongs to another session")
    if (head.revision.revision_id, head.revision.artifact_sha256) != (revision_id, sha256):
        raise ArtifactConflictError("Selected annotation version changed; reopen and select again")
    binding = await get_working_files(service, document_id)
    if (
        binding is None
        or binding.workspace != str(workspace.root)
        or binding.base_revision_id != revision_id
    ):
        raise ArtifactConflictError("Annotation working baseline changed")
    modern = annotation_format(head.document.name, head.revision.media_type)
    previous = await asyncio.to_thread(
        load_version_bundle if modern == "html" else _load_office_version_bundle,
        store,
        head.revision.artifact_id,
        session_id,
    )
    current = await asyncio.to_thread(binding.bundle, read_guard=_source_guard(workspace))
    if _working_bundle_digest(current) != _working_bundle_digest(previous):
        raise ArtifactConflictError("Annotation source changed; reopen and select again")
