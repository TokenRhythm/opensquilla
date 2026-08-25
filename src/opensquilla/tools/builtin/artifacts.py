"""Explicit generated-artifact publication tool."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from opensquilla.artifact_validation import (
    ArtifactValidationError,
    is_pptx_candidate,
    validate_artifact_for_delivery,
)
from opensquilla.artifacts import (
    DEFAULT_ARTIFACT_DISK_BUDGET_BYTES,
    DEFAULT_ARTIFACT_MAX_BYTES,
    ArtifactBudgetError,
    ArtifactBundleManifest,
    ArtifactIntegrityError,
    ArtifactPathError,
    ArtifactStore,
    artifact_bundle_manifest,
    artifact_mime_for_name,
    artifact_payload,
    artifact_publish_max_bytes_for_name,
    collect_artifact_bundle,
)
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.session.plans import (
    PLAN_STEP_TERMINAL_STATUSES,
    PlanRunConflictError,
)
from opensquilla.tools.path_aliases import resolve_workspace_alias
from opensquilla.tools.path_policy import reject_foreign_host_path
from opensquilla.tools.registry import tool
from opensquilla.tools.types import (
    CallerKind,
    RetryableToolInputError,
    ToolContext,
    ToolError,
    current_tool_context,
    is_goal_owned_main_default_turn,
)

_MAX_MISSING_FILE_CANDIDATES = 5
_MAX_MISSING_FILE_SCAN = 2000


def _bundle_result(manifest: ArtifactBundleManifest) -> dict[str, object]:
    return {
        "collection_status": manifest.collection_status,
        "warning_codes": list(manifest.warning_codes),
        "file_count": manifest.file_count,
        "total_bytes": manifest.total_size,
    }


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
    if is_goal_owned_main_default_turn(ctx):
        final_response = (
            "Do not call publish_artifact again for this unchanged file. Follow the "
            "Active Goal instructions: re-evaluate the entire objective and continue "
            "any remaining work with the ordinary tools available for this turn. "
            "update_goal_progress remains optional; use it only when a concise current-state "
            "view helps, and replace that view when reality changes rather than treating it "
            "as fixed phases or turn boundaries. Call "
            "update_goal only when the entire objective is complete or genuinely blocked. "
            "If a terminal Goal update already succeeded in this tool batch, run no more "
            "tools and give one concise final summary."
        )
    else:
        final_response = (
            "Do not run more tools for this deliverable unless the user explicitly "
            "asked for another file or a specific verification step. Send the final "
            "response now."
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
        if is_goal_owned_main_default_turn(ctx):
            return (
                "This file is already registered for the current surface in this turn. "
                + final_response
            )
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


def _plan_run_steps_ready_for_delivery(run: Any) -> bool:
    current_step_id = str(getattr(run, "current_step_id", "") or "")
    step_states = list(getattr(run, "step_states", []) or [])
    return (
        not current_step_id
        and bool(step_states)
        and all(
            isinstance(state, dict)
            and str(state.get("status") or "") in {"completed", "skipped"}
            for state in step_states
        )
    )


def _plan_run_allows_delivery(ctx: ToolContext, run: Any) -> bool:
    """Return whether the current task may deliver from this PlanRun state."""

    status = str(getattr(run, "status", "") or "")
    if status == "completed":
        return True
    if status != "running":
        return False
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    active_task_id = str(getattr(run, "active_task_id", "") or "").strip()
    return (
        bool(task_id)
        and active_task_id == task_id
        and _plan_run_steps_ready_for_delivery(run)
    )


def _plan_run_final_step_ready_for_publish(run: Any) -> str | None:
    """Return the sole unfinished current step that publication can finalize."""

    current_step_id = str(getattr(run, "current_step_id", "") or "")
    if not current_step_id:
        return None
    step_states = list(getattr(run, "step_states", []) or [])
    current_matches = [
        state
        for state in step_states
        if isinstance(state, dict)
        and str(state.get("step_id") or "") == current_step_id
    ]
    if len(current_matches) != 1:
        return None
    if str(current_matches[0].get("status") or "") != "in_progress":
        return None
    if any(
        not isinstance(state, dict)
        or (
            str(state.get("step_id") or "") != current_step_id
            and str(state.get("status") or "") not in PLAN_STEP_TERMINAL_STATUSES
        )
        for state in step_states
    ):
        return None
    return current_step_id


async def _checkpoint_final_plan_step_for_publish(
    ctx: ToolContext,
    run: Any,
) -> Any:
    """Atomically enter delivery when publish is the final step operation."""

    step_id = _plan_run_final_step_ready_for_publish(run)
    if step_id is None:
        return run
    storage = getattr(ctx, "plan_storage", None)
    if storage is None:
        return run
    checkpoint_plan_run = getattr(storage, "checkpoint_plan_run", None)
    if not callable(checkpoint_plan_run):
        return run
    run_id = str(getattr(ctx, "plan_run_id", "") or "").strip()
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    try:
        return await checkpoint_plan_run(
            run_id,
            expected_state_revision=int(getattr(run, "state_revision", 0)),
            step_id=step_id,
            step_status="completed",
            next_step_id=None,
            expected_active_task_id=task_id,
        )
    except PlanRunConflictError:
        refreshed = await storage.get_plan_run(run_id)
        if refreshed is not None and _plan_run_allows_delivery(ctx, refreshed):
            return refreshed
        raise RetryableToolInputError(
            "publish_artifact was not executed because the attached PlanRun "
            "changed while entering artifact delivery. Retry publish_artifact "
            "with the current PlanRun state."
        ) from None


async def _require_plan_run_ready_for_publish(ctx: ToolContext) -> Any | None:
    """Validate delivery state and return a final step to checkpoint if needed."""

    run_id = str(getattr(ctx, "plan_run_id", "") or "").strip()
    if not run_id:
        return None
    storage = getattr(ctx, "plan_storage", None)
    get_plan_run = getattr(storage, "get_plan_run", None)
    if not callable(get_plan_run):
        raise ToolError("PlanRun storage is unavailable for artifact publication")
    run = await get_plan_run(run_id)
    if run is None:
        raise ToolError("The active PlanRun no longer exists")
    status = str(getattr(run, "status", "") or "")
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    active_task_id = str(getattr(run, "active_task_id", "") or "").strip()
    if _plan_run_allows_delivery(ctx, run):
        return None
    if status == "running":
        if not task_id or active_task_id != task_id:
            raise ToolError(
                "Artifact publication is unavailable because this task no longer "
                "owns the attached PlanRun."
            )
        if _plan_run_steps_ready_for_delivery(run):
            return None
        if _plan_run_final_step_ready_for_publish(run) is not None:
            return run
    current_step_id = str(getattr(run, "current_step_id", "") or "")
    current_detail = (
        f" The current step is {current_step_id}."
        if current_step_id
        else ""
    )
    message = (
        "publish_artifact was not executed because the attached PlanRun is "
        f"{status or 'unavailable'}.{current_detail}"
    )
    if status == "running":
        raise RetryableToolInputError(
            f"{message} Record truthful checkpoints for the current step in plan "
            "order, then retry publish_artifact only after the final checkpoint "
            "returns no current step."
        )
    raise ToolError(
        f"{message} Artifact publication is unavailable for this terminal or "
        "unowned PlanRun state."
    )


@tool(
    name="publish_artifact",
    description=(
        "Register an existing workspace file as a generated artifact for the current surface. "
        "Only files inside the active workspace are allowed. "
        "For exactly one file, including self-contained HTML, set bundle='none' and omit "
        "bundle_root. bundle_root is valid only with bundle='directory'. "
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
        "bundle": {
            "type": "string",
            "enum": ["auto", "directory", "none"],
            "description": (
                "Static webpage packaging mode. Use none for exactly one file, including "
                "self-contained HTML, and omit bundle_root. Use auto to follow statically "
                "discoverable local dependencies for an HTML entrypoint, also without "
                "bundle_root. Use directory to snapshot a dedicated workspace subdirectory; "
                "directory requires bundle_root. Defaults to auto."
            ),
        },
        "bundle_root": {
            "type": "string",
            "description": (
                "Required only when bundle=directory: a dedicated workspace subdirectory "
                "containing path. Invalid when bundle is auto or none; omit it in those modes."
            ),
        },
    },
    required=["path"],
    sandbox=SandboxToolDescriptor.artifact(kind="artifact.publish"),
)
async def publish_artifact(
    path: str,
    name: str | None = None,
    mime: str | None = None,
    bundle: str = "auto",
    bundle_root: str | None = None,
) -> str:
    # Some provider adapters materialize omitted optional strings as blank values.
    # Treat those wire-equivalent values as absent without accepting a real root in
    # modes where it is forbidden.
    if bundle_root is not None and not bundle_root.strip():
        bundle_root = None
    ctx = current_tool_context.get()
    if ctx is None:
        raise ToolError("publish_artifact requires tool context")
    final_step_to_checkpoint = await _require_plan_run_ready_for_publish(ctx)
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
    target_candidate = (
        alias_target or (raw_path if raw_path.is_absolute() else workspace / raw_path)
    )
    target = target_candidate.resolve()
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise ToolError(f"artifact path is outside workspace: {path}") from exc
    if not target.exists():
        raise _missing_artifact_error(path, workspace, target)
    if not target.is_file():
        raise ToolError(f"artifact path is not a file: {path}")

    bundle_root_candidate: Path | None = None
    if bundle_root is not None:
        reject_foreign_host_path(bundle_root, platform=os.name, workspace=workspace)
        raw_bundle_root = Path(bundle_root)
        alias_bundle_root = resolve_workspace_alias(raw_bundle_root, workspace)
        bundle_root_candidate = alias_bundle_root or (
            raw_bundle_root
            if raw_bundle_root.is_absolute()
            else workspace / raw_bundle_root
        )
        resolved_bundle_root = bundle_root_candidate.resolve()
        try:
            resolved_bundle_root.relative_to(workspace)
        except ValueError as exc:
            raise ToolError(
                f"artifact bundle root is outside workspace: {bundle_root}"
            ) from exc

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
    try:
        bundle_snapshot = await asyncio.to_thread(
            collect_artifact_bundle,
            target_candidate,
            workspace_root=workspace,
            mode=bundle,
            bundle_root=bundle_root_candidate,
            entry_mime=artifact_mime,
        )
    except (ArtifactBudgetError, ArtifactPathError, OSError) as exc:
        raise ToolError(str(exc)) from exc
    bundle_manifest = (
        artifact_bundle_manifest(bundle_snapshot)
        if bundle_snapshot is not None
        else None
    )
    if bundle_manifest is not None:
        target_sha256 = next(
            item.sha256
            for item in bundle_manifest.files
            if item.path == bundle_manifest.entrypoint
        )
    if final_step_to_checkpoint is not None:
        checkpointed_run = await _checkpoint_final_plan_step_for_publish(
            ctx,
            final_step_to_checkpoint,
        )
        if not _plan_run_steps_ready_for_delivery(checkpointed_run):
            raise RetryableToolInputError(
                "publish_artifact was not executed because the attached PlanRun "
                "could not enter artifact delivery. Retry after checkpointing the "
                "current final step."
            )
    store = ArtifactStore(ctx.artifact_media_root)
    for published in reversed(ctx.published_artifacts):
        if published.get("sha256") != target_sha256:
            continue
        artifact_id = published.get("id")
        if not isinstance(artifact_id, str):
            continue
        try:
            published_manifest = store.describe_preview_bundle(
                artifact_id,
                session_id=ctx.artifact_session_id,
            )
        except (ArtifactIntegrityError, ArtifactPathError, ValueError):
            continue
        if bundle_manifest is None:
            if published_manifest is not None:
                continue
        elif (
            published_manifest is None
            or published_manifest.bundle_digest != bundle_manifest.bundle_digest
        ):
            continue
        llm_artifact = _llm_artifact_payload(
            published,
            ctx=ctx,
            workspace=workspace,
            target=target,
        )
        result: dict[str, object] = {
            "status": "already_published",
            "artifact": llm_artifact,
            "note": _publish_note(ctx, already_published=True),
        }
        if published_manifest is not None:
            result["bundle"] = _bundle_result(published_manifest)
        return json.dumps(result, ensure_ascii=False)

    existing = store.find_existing_ref(
        session_id=ctx.artifact_session_id,
        session_key=ctx.session_key,
        sha256=target_sha256,
        name=artifact_name,
        mime=artifact_mime,
        bundle_digest=(
            bundle_manifest.bundle_digest if bundle_manifest is not None else None
        ),
        require_single_file=bundle_manifest is None,
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
        result = {
            "status": "already_published",
            "artifact": llm_artifact,
            "note": _publish_note(ctx, already_published=True),
        }
        if bundle_manifest is not None:
            result["bundle"] = _bundle_result(bundle_manifest)
        return json.dumps(result, ensure_ascii=False)
    disk_budget_bytes = (
        ctx.artifact_disk_budget_bytes
        if ctx.artifact_disk_budget_bytes is not None
        else DEFAULT_ARTIFACT_DISK_BUDGET_BYTES
    )
    try:
        if bundle_snapshot is not None:
            ref = store.publish_bundle(
                bundle_snapshot,
                session_id=ctx.artifact_session_id,
                session_key=ctx.session_key,
                name=artifact_name,
                mime=artifact_mime,
                source="publish_artifact",
                max_bytes=max_bytes,
                disk_budget_bytes=disk_budget_bytes,
            )
        elif target_is_pptx:
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
    result = {
            "status": "published",
            "artifact": llm_artifact,
            "note": _publish_note(ctx),
    }
    if bundle_manifest is not None:
        result["bundle"] = _bundle_result(bundle_manifest)
    return json.dumps(result, ensure_ascii=False)
