"""Ordinary page and selection context carried by a user message."""

from __future__ import annotations

import html
import json
from pathlib import PurePosixPath
from typing import Any, cast


def normalize_page_context(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= {
        "targetRef",
        "resourceId",
        "pagePath",
        "annotations",
    }:
        raise ValueError("pageContext accepts page references and annotations only")
    result: dict[str, Any] = {}
    for key in ("targetRef", "resourceId"):
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or not item.strip() or len(item.encode()) > 512:
                raise ValueError(f"Invalid pageContext.{key}")
            result[key] = item.strip()
    page_path = value.get("pagePath")
    if page_path is not None:
        if (
            not isinstance(page_path, str)
            or not page_path.strip()
            or len(page_path.encode()) > 4096
            or PurePosixPath(page_path).is_absolute()
            or ".." in PurePosixPath(page_path).parts
            or any(character in page_path for character in ("\\", ":", "\x00"))
            or not result.get("resourceId")
        ):
            raise ValueError("Invalid pageContext.pagePath")
        result["pagePath"] = page_path
    annotations = value.get("annotations", [])
    if not isinstance(annotations, list) or len(annotations) > 16:
        raise ValueError("pageContext supports at most 16 annotations")
    normalized = []
    for annotation in annotations:
        if not isinstance(annotation, dict) or not set(annotation) <= {
            "text",
            "selectionText",
            "locatorHint",
        }:
            raise ValueError("Invalid page annotation")
        current = {}
        for key in ("text", "selectionText", "locatorHint"):
            item = annotation.get(key)
            if item is not None:
                if not isinstance(item, str) or len(item.encode()) > 16384:
                    raise ValueError("Page annotation exceeds its text limit")
                current[key] = item
        if not current.get("text", "").strip():
            raise ValueError("Page annotation requires text")
        normalized.append(current)
    if normalized:
        result["annotations"] = normalized
    if len(json.dumps(result, ensure_ascii=False).encode()) > 65536:
        raise ValueError("Page context exceeds its size limit")
    return result or None


def render_page_context(value: dict[str, Any]) -> str:
    return (
        "<page_context>\n"
        + html.escape(
            json.dumps(value, ensure_ascii=False),
            quote=False,
        )
        + "\n</page_context>"
    )


async def resolve_page_context(
    value: dict[str, Any],
    ctx: Any,
    *,
    session_key: str,
    session_id: str,
    workspace: str,
) -> dict[str, Any]:
    """Resolve a session resource to normal workspace paths, without tool policy changes."""
    result = dict(value)
    resource_id = value.get("resourceId")
    if not resource_id:
        return result
    kind, separator, identity = resource_id.partition(":")
    if not separator or kind not in {"document", "attachment", "deliverable"} or not identity:
        raise ValueError("Invalid page resource reference")
    from opensquilla.application.artifact_workbench import (
        WorkbenchResourceApplication,
        WorkbenchResourceOpen,
        WorkbenchResourcePort,
        WorkbenchResourceRef,
    )
    from opensquilla.artifact_session import ArtifactSessionService
    from opensquilla.artifact_session.working_files import checked_path, ensure_working_files
    from opensquilla.artifacts import ArtifactStore
    from opensquilla.gateway.session_services import get_session_storage
    from opensquilla.gateway.workbench_resource_runtime import _WorkbenchResourceRuntimePort
    from opensquilla.paths import media_root_from_config

    port = cast(WorkbenchResourcePort, _WorkbenchResourceRuntimePort(ctx))
    opened = await WorkbenchResourceApplication(port).open(
        WorkbenchResourceOpen(session_key, WorkbenchResourceRef(kind, identity))
    )
    document = opened.get("document")
    if not isinstance(document, dict) or not document.get("documentId"):
        return result
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise ValueError("Page resources require session storage")
    service = await ArtifactSessionService.from_session_storage(storage)
    try:
        binding = await ensure_working_files(
            service,
            ArtifactStore(media_root_from_config(ctx.config)),
            document_id=document["documentId"],
            session_key=session_key,
            session_id=session_id,
            workspace=workspace,
        )
        result["resourceId"] = f"document:{binding.document_id}"
        working_file = binding.entry
        if page_path := value.get("pagePath"):
            candidate = checked_path(binding.root, page_path)
            # A page hint selects an existing HTML member, never another source root.
            # bundle() also revalidates and safely reads the current source files.
            member = next((item for item in binding.bundle().files if item.path == page_path), None)
            if member is None or member.mime.split(";", 1)[0].strip().lower() not in {
                "text/html", "application/xhtml+xml",
            }:
                raise ValueError("Page context must reference an HTML page in the document")
            working_file = binding.entry if page_path == binding.entrypoint else candidate
        result["workingFile"] = str(working_file)
        result["workingDirectory"] = str(binding.root)
        result["versionId"] = binding.base_revision_id
        return result
    finally:
        await service.close()
