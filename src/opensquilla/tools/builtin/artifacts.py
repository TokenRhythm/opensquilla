"""Explicit generated-artifact publication tool."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from difflib import SequenceMatcher
from pathlib import Path

from opensquilla.artifact_validation import (
    ArtifactValidationError,
    is_pptx_candidate,
    validate_artifact_for_delivery,
)
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactBudgetError,
    ArtifactStore,
    artifact_mime_for_name,
    artifact_payload,
    artifact_publish_max_bytes_for_name,
)
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.path_aliases import resolve_workspace_alias
from opensquilla.tools.path_policy import reject_foreign_host_path
from opensquilla.tools.registry import tool
from opensquilla.tools.types import (
    CallerKind,
    RetryableToolInputError,
    ToolContext,
    ToolError,
    current_tool_context,
)

_MAX_MISSING_FILE_CANDIDATES = 5
_MAX_MISSING_FILE_SCAN = 2000


def _normalized_filename(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _artifact_candidate_paths(
    workspace: Path,
    requested: Path,
    *,
    limit: int = _MAX_MISSING_FILE_CANDIDATES,
    max_scan: int = _MAX_MISSING_FILE_SCAN,
) -> list[str]:
    requested_name = requested.name
    if not requested_name:
        return []
    requested_norm = _normalized_filename(requested_name)
    requested_suffix = requested.suffix.lower()
    scored: list[tuple[float, str]] = []
    scanned = 0
    for candidate in workspace.rglob("*"):
        scanned += 1
        if scanned > max_scan:
            break
        if not candidate.is_file():
            continue
        candidate_name = candidate.name
        candidate_norm = _normalized_filename(candidate_name)
        score = 0.0
        if candidate_name == requested_name:
            score = 1.0
        elif candidate_name.lower() == requested_name.lower():
            score = 0.95
        elif requested_norm and candidate_norm == requested_norm:
            score = 0.9
        elif requested_suffix and candidate.suffix.lower() == requested_suffix:
            score = SequenceMatcher(None, requested_norm, candidate_norm).ratio()
        if score < 0.55:
            continue
        rel = candidate.relative_to(workspace).as_posix()
        scored.append((score, rel))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _, path in scored[:limit]]


def _missing_artifact_error(path: str, workspace: Path, target: Path) -> ToolError:
    candidates = _artifact_candidate_paths(workspace, Path(path))
    details = [
        f"artifact file not found: {path}",
        f"active workspace: {workspace}",
        f"resolved path: {target}",
    ]
    if candidates:
        details.append("candidate files: " + ", ".join(candidates))
    else:
        details.append("candidate files: none found")
    return ToolError(". ".join(details))


def _should_expose_local_path(ctx: ToolContext) -> bool:
    return bool(ctx.is_owner and ctx.caller_kind in {CallerKind.CLI, CallerKind.WEB})


def _llm_artifact_payload(
    payload: dict[str, object],
    *,
    ctx: ToolContext,
    workspace: Path,
    target: Path,
) -> dict[str, object]:
    llm_artifact = {k: v for k, v in payload.items() if k != "download_url"}
    if _should_expose_local_path(ctx):
        workspace_path = target.relative_to(workspace).as_posix()
        llm_artifact["workspace_path"] = workspace_path
        llm_artifact["local_path"] = str(target)
    return llm_artifact


def _publish_note(ctx: ToolContext, *, already_published: bool = False) -> str:
    final_response = (
        "Do not run more tools for this deliverable unless the user explicitly "
        "asked for another file or a specific verification step. Send the final response now."
    )
    if _should_expose_local_path(ctx):
        prefix = (
            "This file is already registered for the current surface in this turn. "
            if already_published
            else "The user already sees a clickable download button rendered by the UI. "
        )
        return (
            prefix
            + "Do not include any artifact URL in your reply. "
            + "Mention the local_path as the local entry path when the user needs to open "
            + f"the generated file on this machine. {final_response}"
        )
    if already_published:
        return (
            "This file is already registered for the current surface in this turn. "
            "Do not call publish_artifact again for the same file; just confirm it is ready. "
            + final_response
        )
    return (
        "The active surface handles artifact download or native channel delivery. "
        f"Do not include any URL in your reply. {final_response}"
    )


def _publish_artifact_metadata(
    *,
    target: Path,
    name: str | None,
    mime: str | None,
) -> tuple[str, str]:
    artifact_name = (name or target.name).strip() or target.name
    if name and not Path(artifact_name).suffix and target.suffix:
        artifact_name = f"{artifact_name}{target.suffix}"

    if mime:
        artifact_mime = mime.strip()
    else:
        target_mime = artifact_mime_for_name(target.name)
        artifact_name_mime = artifact_mime_for_name(artifact_name)
        artifact_mime = target_mime or artifact_name_mime or ""
        if target_mime == "application/octet-stream" and artifact_name_mime:
            artifact_mime = artifact_name_mime
    if not artifact_mime:
        artifact_mime = "application/octet-stream"
    return artifact_name, artifact_mime


@tool(
    name="publish_artifact",
    description=(
        "Register an existing workspace file as a generated artifact for the current surface. "
        "Only files inside the active workspace are allowed. "
        "The active surface handles download chips or native channel delivery; do not include "
        "any URL in your reply — just confirm the file is ready."
    ),
    params={
        "path": {
            "type": "string",
            "description": "Workspace-relative or in-workspace absolute path to publish.",
        },
        "name": {
            "type": "string",
            "description": "Optional download filename. Defaults to the source filename.",
        },
        "mime": {
            "type": "string",
            "description": "Optional MIME type. Defaults to a filename guess.",
        },
    },
    required=["path"],
    sandbox=SandboxToolDescriptor.artifact(kind="artifact.publish"),
)
async def publish_artifact(
    path: str,
    name: str | None = None,
    mime: str | None = None,
) -> str:
    ctx = current_tool_context.get()
    if ctx is None:
        raise ToolError("publish_artifact requires tool context")
    if not ctx.workspace_dir:
        raise ToolError("publish_artifact requires an active workspace")
    if not ctx.artifact_media_root:
        raise ToolError("artifact storage is not configured for this turn")
    if not ctx.artifact_session_id or not ctx.session_key:
        raise ToolError("artifact session scope is not configured for this turn")

    workspace = Path(ctx.workspace_dir).resolve()
    reject_foreign_host_path(path, platform=os.name, workspace=workspace)
    raw_path = Path(path)
    alias_target = resolve_workspace_alias(raw_path, workspace)
    target = (
        alias_target or (raw_path if raw_path.is_absolute() else workspace / raw_path)
    ).resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ToolError(f"artifact path is outside workspace: {path}") from exc
    if not target.exists():
        raise _missing_artifact_error(path, workspace, target)
    if not target.is_file():
        raise ToolError(f"artifact path is not a file: {path}")

    artifact_name, artifact_mime = _publish_artifact_metadata(
        target=target,
        name=name,
        mime=mime,
    )
    target_is_pptx = is_pptx_candidate(
        source_name=target.name,
        name=artifact_name,
        mime=artifact_mime,
    )
    configured_max_bytes = (
        ctx.artifact_max_bytes
        if ctx.artifact_max_bytes is not None
        else DEFAULT_ARTIFACT_MAX_BYTES
    )
    max_bytes = artifact_publish_max_bytes_for_name(
        artifact_name,
        configured_max_bytes,
    )
    target_payload: bytes | None = None
    if target_is_pptx:
        target_size = target.stat().st_size
        if max_bytes is not None and target_size > max_bytes:
            budget_error = ArtifactBudgetError(
                "artifact exceeds per-file budget "
                f"({target_size} > {max_bytes})"
            )
            raise ToolError(str(budget_error)) from budget_error
        # Inflation plus full-deck parsing is CPU-bound; keep it off the
        # gateway event loop so concurrent sessions stay responsive.
        target_payload = await asyncio.to_thread(target.read_bytes)
        try:
            await asyncio.to_thread(
                validate_artifact_for_delivery,
                target_payload,
                source_name=target.name,
                name=artifact_name,
                mime=artifact_mime,
                source="publish_artifact",
            )
        except ArtifactValidationError as exc:
            raise RetryableToolInputError(exc.user_message) from exc

    target_sha256 = hashlib.sha256(
        target_payload if target_payload is not None else target.read_bytes()
    ).hexdigest()
    for published in reversed(ctx.published_artifacts):
        if published.get("sha256") != target_sha256:
            continue
        llm_artifact = _llm_artifact_payload(
            published,
            ctx=ctx,
            workspace=workspace,
            target=target,
        )
        return json.dumps(
            {
                "status": "already_published",
                "artifact": llm_artifact,
                "note": _publish_note(ctx, already_published=True),
            },
            ensure_ascii=False,
        )

    store = ArtifactStore(ctx.artifact_media_root)
    existing = store.find_existing_ref(
        session_id=ctx.artifact_session_id,
        session_key=ctx.session_key,
        sha256=target_sha256,
        name=artifact_name,
        mime=artifact_mime,
    )
    if existing is not None:
        payload = artifact_payload(existing)
        if not any(item.get("id") == payload.get("id") for item in ctx.published_artifacts):
            ctx.published_artifacts.append(payload)
        llm_artifact = _llm_artifact_payload(
            payload,
            ctx=ctx,
            workspace=workspace,
            target=target,
        )
        return json.dumps(
            {
                "status": "already_published",
                "artifact": llm_artifact,
                "note": _publish_note(ctx, already_published=True),
            },
            ensure_ascii=False,
        )
    disk_budget_bytes = (
        ctx.artifact_disk_budget_bytes
        if ctx.artifact_disk_budget_bytes is not None
        else DEFAULT_ARTIFACT_DISK_BUDGET_BYTES
    )
    try:
        if target_is_pptx:
            assert target_payload is not None
            ref = store.publish_bytes(
                target_payload,
                session_id=ctx.artifact_session_id,
                session_key=ctx.session_key,
                name=artifact_name,
                mime=artifact_mime,
                source="publish_artifact",
                max_bytes=max_bytes,
                disk_budget_bytes=disk_budget_bytes,
            )
        else:
            ref = store.publish_file(
                target,
                session_id=ctx.artifact_session_id,
                session_key=ctx.session_key,
                name=artifact_name,
                mime=artifact_mime,
                source="publish_artifact",
                max_bytes=max_bytes,
                disk_budget_bytes=disk_budget_bytes,
            )
    except ArtifactBudgetError as exc:
        raise ToolError(str(exc)) from exc
    except FileNotFoundError as exc:
        if not target.exists():
            raise _missing_artifact_error(path, workspace, target) from exc
        raise ToolError(f"artifact storage path is unavailable: {exc}") from exc

    payload = artifact_payload(ref)
    ctx.published_artifacts.append(payload)
    llm_artifact = _llm_artifact_payload(
        payload,
        ctx=ctx,
        workspace=workspace,
        target=target,
    )
    return json.dumps(
        {
            "status": "published",
            "artifact": llm_artifact,
            "note": _publish_note(ctx),
        },
        ensure_ascii=False,
    )
