"""Ordinary page and selection context carried by a user message."""

from __future__ import annotations

import html
import json
import math
import re
from pathlib import PurePosixPath
from typing import Any, cast


def normalize_office_selection(value: object) -> dict[str, Any]:
    """Keep bounded renderer evidence, never file paths, tool policy or write handles."""
    if not isinstance(value, dict):
        raise ValueError("Invalid Office selection")
    format = value.get("format")
    kind = value.get("kind")
    if (
        not isinstance(format, str)
        or not isinstance(kind, str)
        or format not in {"docx", "pptx", "xlsx"}
        or kind not in {"text", "element", "range"}
    ):
        raise ValueError("Invalid Office selection format or kind")
    if (format == "xlsx" and kind == "text") or (format != "xlsx" and kind == "range"):
        raise ValueError("Invalid Office selection kind for format")
    strings = {"text": 16384, "shapeId": 256, "name": 256, "sheetName": 256, "range": 128}
    integers = {"pageIndex", "slideIndex", "sheetIndex"}
    arrays = {
        "pageIndexes": (32, 100000),
        "slideIndexes": (32, 100000),
        "paragraphIds": (64, 256),
        "shapeIds": (64, 256),
        "ranges": (16, 128),
    }
    enums = {
        "origin": {"master", "layout", "slide", "unknown"},
        "elementType": {"chart", "image", "picture", "shape", "table", "media", "group"},
    }
    allowed = (
        set(strings)
        | integers
        | set(arrays)
        | set(enums)
        | {
            "format",
            "kind",
            "source",
            "bounds",
            "truncated",
        }
    )
    if not set(value) <= allowed:
        raise ValueError("Unsupported Office selection field")

    def integer(item: object, maximum: int = 100000) -> bool:
        return type(item) is int and 0 <= item <= maximum

    for key, item in value.items():
        valid = True
        if key in strings:
            valid = isinstance(item, str) and len(item.encode()) <= strings[key]
        elif key in integers:
            valid = integer(item)
        elif key in arrays:
            count, size = arrays[key]
            valid = (
                isinstance(item, list)
                and len(item) <= count
                and all(
                    integer(entry, size)
                    if key.endswith("Indexes")
                    else isinstance(entry, str) and len(entry.encode()) <= size
                    for entry in item
                )
            )
        elif key in enums:
            valid = isinstance(item, str) and item in enums[key]
        elif key == "truncated":
            valid = type(item) is bool
        elif key == "source":
            valid = isinstance(item, dict) and set(item) <= {"story", "storyInstance", "path"}
            if valid:
                valid = (
                    isinstance(item.get("story"), str)
                    and item.get("story")
                    in {"body", "header", "footer", "footnote", "endnote", "textbox"}
                    and (
                        "storyInstance" not in item
                        or isinstance(item["storyInstance"], str)
                        and len(item["storyInstance"].encode()) <= 256
                    )
                    and (
                        "path" not in item
                        or isinstance(item["path"], list)
                        and len(item["path"]) <= 32
                        and all(integer(i) for i in item["path"])
                    )
                )
        elif key == "bounds":
            valid = isinstance(item, dict) and set(item) <= {
                "x",
                "y",
                "width",
                "height",
                "rotation",
                "flipH",
                "flipV",
                "xPt",
                "yPt",
                "widthPt",
                "heightPt",
            }
            if valid:
                valid = all(
                    type(v) is bool
                    if k in {"flipH", "flipV"}
                    else type(v) in {int, float} and abs(v) <= 1e9 and math.isfinite(v)
                    for k, v in item.items()
                )
        if not valid:
            raise ValueError(f"Invalid Office selection.{key}")
    return value.copy()


def normalize_page_context(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not set(value) <= {
        "targetRef",
        "resourceId",
        "pagePath",
        "annotations",
        "revisionId",
        "sha256",
    }:
        raise ValueError("pageContext accepts page references and annotations only")
    result: dict[str, Any] = {}
    for key in ("targetRef", "resourceId", "revisionId"):
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or not item.strip() or len(item.encode()) > 512:
                raise ValueError(f"Invalid pageContext.{key}")
            result[key] = item.strip()
    sha = value.get("sha256")
    if sha is not None:
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha) is None:
            raise ValueError("Invalid pageContext.sha256")
        result["sha256"] = sha.lower()
    if bool(result.get("revisionId")) != bool(result.get("sha256")):
        raise ValueError("Page context requires both revisionId and sha256")
    if result.get("revisionId") and not result.get("resourceId"):
        raise ValueError("Page context version requires a resourceId")
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
            "selection",
        }:
            raise ValueError("Invalid page annotation")
        current: dict[str, Any] = {}
        for key in ("text", "selectionText", "locatorHint"):
            item = annotation.get(key)
            if item is not None:
                if not isinstance(item, str) or len(item.encode()) > 16384:
                    raise ValueError("Page annotation exceeds its text limit")
                current[key] = item
        if "selection" in annotation:
            current["selection"] = normalize_office_selection(annotation["selection"])
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
    candidate_id: str | None = None,
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
    from opensquilla.artifact_session import ArtifactConflictError, ArtifactSessionService
    from opensquilla.artifact_session.working_files import checked_path, ensure_working_files
    from opensquilla.artifacts import ArtifactStore
    from opensquilla.gateway.session_services import get_session_storage
    from opensquilla.gateway.workbench_resource_runtime import _WorkbenchResourceRuntimePort
    from opensquilla.paths import media_root_from_config

    port = cast(WorkbenchResourcePort, _WorkbenchResourceRuntimePort(ctx))
    opened = await WorkbenchResourceApplication(port).open(
        WorkbenchResourceOpen(
            session_key,
            WorkbenchResourceRef(kind, identity),
            expected_sha256=value.get("sha256"),
        )
    )
    document = opened.get("document")
    if not isinstance(document, dict) or not document.get("documentId"):
        return result
    storage = get_session_storage(ctx.session_manager)
    if storage is None:
        raise ValueError("Page resources require session storage")
    service = await ArtifactSessionService.from_session_storage(storage)
    try:
        head = await service.get_document_head(document["documentId"])
        if value.get("revisionId") and (
            value["revisionId"] != head.revision.revision_id
            or value["sha256"] != head.revision.artifact_sha256
        ):
            raise ValueError("The selected document version changed. Reopen it and select again.")
        binding = await ensure_working_files(
            service,
            ArtifactStore(media_root_from_config(ctx.config)),
            document_id=document["documentId"],
            session_key=session_key,
            session_id=session_id,
            workspace=workspace,
        )
        if value.get("annotations") and value.get("revisionId"):
            from opensquilla.gateway.annotation_preparation import validate_annotation_source
            from opensquilla.gateway.workspace_files import WorkspaceFiles

            selected_workspace = await WorkspaceFiles(
                ctx.config, ctx.session_manager,
            ).workspace(session_key)
            try:
                await validate_annotation_source(
                    service, ArtifactStore(media_root_from_config(ctx.config)),
                    document_id=binding.document_id, session_key=session_key,
                    session_id=session_id, revision_id=value["revisionId"],
                    sha256=value["sha256"], workspace=selected_workspace,
                )
            except ArtifactConflictError as exc:
                from opensquilla.gateway.artifact_product_errors import (
                    ArtifactProductErrorCode,
                    artifact_product_error,
                )

                raise artifact_product_error(
                    ArtifactProductErrorCode.DOCUMENT_CHANGED,
                ) from exc
            result["_annotationSelection"] = {
                "documentId": binding.document_id,
                "revisionId": value["revisionId"], "sha256": value["sha256"],
            }
        result["resourceId"] = f"document:{binding.document_id}"
        from opensquilla.office_format import office_format

        if modern := office_format(head.document.name, head.revision.media_type):
            if not value.get("revisionId") or not candidate_id or value.get("pagePath"):
                raise ValueError("Office changes require the selected revisionId and sha256")
            if any(
                annotation.get("selection", {}).get("format", modern) != modern
                for annotation in value.get("annotations", [])
            ):
                raise ValueError("Office selection belongs to another format")
            from opensquilla.artifact_session.working_files import office_candidate_path

            candidate = {
                "documentId": binding.document_id,
                "revisionId": head.revision.revision_id,
                "sha256": head.revision.artifact_sha256,
                "stateRevision": head.document.state_revision,
                "candidateId": candidate_id,
            }
            path = office_candidate_path(binding, candidate)
            result.update(
                {
                    "workingFile": str(path),
                    "workingDirectory": str(path.parent),
                    "versionId": head.revision.revision_id,
                    "_officeCandidate": candidate,
                    "editingInstructions": (
                        "Modify only this candidate file. Selection geometry and renderer indexes "
                        "are context, not write handles. Resolve the quoted content and OOXML "
                        "identifiers in this file. If ambiguous, ask rather than guess. "
                        "Preserve unrelated content. "
                        "The host validates and commits the candidate after successful completion."
                    ),
                }
            )
            return result
        working_file = binding.entry
        if page_path := value.get("pagePath"):
            page_candidate = checked_path(binding.root, page_path)
            # A page hint selects an existing HTML member, never another source root.
            # bundle() also revalidates and safely reads the current source files.
            member = next((item for item in binding.bundle().files if item.path == page_path), None)
            if member is None or member.mime.split(";", 1)[0].strip().lower() not in {
                "text/html",
                "application/xhtml+xml",
            }:
                raise ValueError("Page context must reference an HTML page in the document")
            working_file = binding.entry if page_path == binding.entrypoint else page_candidate
        result["workingFile"] = str(working_file)
        result["workingDirectory"] = str(binding.root)
        result["versionId"] = binding.base_revision_id
        return result
    finally:
        await service.close()
