"""Copy a fork's editable attachment through the ordinary filesystem boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from opensquilla.attachment_workspace import _safe_path_segment
from opensquilla.sandbox.integration import active_file_system_profile
from opensquilla.sandbox.operation_runtime import SandboxOperation
from opensquilla.tools.builtin import filesystem
from opensquilla.tools.types import ToolContext, current_tool_context


def _boundary(context: ToolContext, workspace: Path) -> dict[str, object]:
    if not context.artifact_session_id:
        raise PermissionError("attachment fork requires a session boundary")
    base = workspace / ".opensquilla" / "attachments"
    return {
        "workspaceStrict": True,
        "attachmentBase": str(base),
        "attachmentSessionRoot": str(
            base / _safe_path_segment(context.artifact_session_id, fallback="session")
        ),
    }


async def copy_fork_working_file(
    source: Path,
    target: Path,
    *,
    source_context: ToolContext,
    target_context: ToolContext,
) -> None:
    """Require both current policies, with no elevation or Safe host fallback."""
    source_root = Path(source_context.workspace_dir or "").resolve(strict=True)
    target_root = Path(target_context.workspace_dir or "").resolve(strict=True)
    token = current_tool_context.set(source_context)
    try:
        blocked = filesystem._sensitive_access_block("read_file", source, str(source))
        blocked = blocked or filesystem._sandbox_path_access_envelope(source, write=False)
        if blocked is not None:
            raise PermissionError("attachment fork source read is denied")
        filesystem._gate_workspace_strict_read("read_file", source, str(source))
        source_profile = active_file_system_profile(source_root)
        source_mode = filesystem._active_filesystem_run_mode()
    finally:
        current_tool_context.reset(token)

    token = current_tool_context.set(target_context)
    try:
        blocked, elevated, _ = await filesystem._gate_out_of_workspace_write(
            "write_file", target, str(target), None,
        )
        if blocked is not None or elevated:
            raise PermissionError("attachment fork target write is denied")
        target_profile = active_file_system_profile(target_root)
        target_mode = filesystem._active_filesystem_run_mode()
        # Forks retain their workspace and inherited context. Refuse a changed
        # policy instead of combining profiles into an accidental permission grant.
        if source_root != target_root or source_profile != target_profile:
            raise PermissionError("attachment fork filesystem authority changed")
        run_mode = "full" if source_mode == target_mode == "full" else "safe"
        operation = SandboxOperation.filesystem(
            kind="fork_attachment", workspace=target_root, run_mode=run_mode,
            path=target, paths=(target,), source_path=source,
            file_system_profile=target_profile,
        )
        operation = replace(
            operation,
            permissions=replace(operation.permissions, filesystem={
                **_boundary(target_context, target_root),
                "forkSourceBoundary": _boundary(source_context, source_root),
            }),
        )
        result = await filesystem._run_sandbox_operation_if_required(operation)
        if result is None:
            if run_mode != "full":
                raise PermissionError("attachment fork requires an available filesystem sandbox")
            from opensquilla.sandbox.filesystem_worker import _run

            payload = operation.to_payload()
            payload["_filesystemProfileCache"] = target_profile
            await asyncio.to_thread(_run, payload)
    finally:
        current_tool_context.reset(token)
