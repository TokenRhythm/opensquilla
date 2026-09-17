"""Open current workspace HTML without creating a public deliverable."""

from __future__ import annotations

import json

from opensquilla.artifact_session.errors import ArtifactConflictError
from opensquilla.project_workspaces import ProjectWorkspaceStateError
from opensquilla.sandbox.operation_runtime import SandboxToolDescriptor
from opensquilla.tools.registry import tool
from opensquilla.tools.types import PlanAccess, ToolError, current_tool_context


@tool(
    name="open_workspace_preview",
    description=(
        "Register an HTML page from the active workspace and request the client to open it "
        "for preview and visual feedback. "
        "Keeps the original source files. This does not build, start a server, or end the turn. "
        "Omit bundle options when reopening an existing page to retain its collection scope. "
        "previewStatus reports resource readiness, not confirmation that a client displayed it."
    ),
    params={
        "path": {"type": "string", "description": "HTML entry file in the active workspace."},
        "bundle": {
            "type": "string", "enum": ["auto", "directory", "none"],
            "description": (
                "Use directory with a dedicated bundle_root for a generated site. "
                "New pages default to auto. Existing pages retain their registered mode."
            ),
        },
        "bundle_root": {
            "type": "string",
            "description": "Dedicated workspace subdirectory; required only for directory mode.",
        },
    },
    required=["path"],
    owner_only=True,
    plan_access=PlanAccess.READ_ONLY,
    cancellation_policy="must_settle",
    sandbox=SandboxToolDescriptor.artifact(kind="artifact.preview"),
)
async def open_workspace_preview(
    path: str, bundle: str | None = None, bundle_root: str | None = None,
) -> str:
    context = current_tool_context.get()
    if context is None or context.workspace_preview_opener is None:
        raise ToolError("WORKSPACE_PREVIEW_UNAVAILABLE: No workspace preview service is available.")
    if not context.is_owner or context.guest_safe:
        raise ToolError("WORKSPACE_PREVIEW_UNAVAILABLE: This session cannot open workspace pages.")
    try:
        result = await context.workspace_preview_opener(
            context, path=path, bundle=bundle, bundle_root=bundle_root,
        )
    except (ArtifactConflictError, ProjectWorkspaceStateError, OSError, ValueError) as exc:
        raise ToolError(f"WORKSPACE_PREVIEW_FAILED: {exc}") from exc
    return json.dumps(result, ensure_ascii=False)
